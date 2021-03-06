import logging
import os
import numpy as np
import math
import scipy.sparse as ssp
from pyspark.sql import functions as f
from sklearn.random_projection import johnson_lindenstrauss_min_dim, SparseRandomProjection
from pyspark.sql import SparkSession
from pyspark.sql.types import *
from pyspark.ml.linalg import Vectors, VectorUDT
from sklearn.externals import joblib


# to run this code use: $SPARK_HOME/spark-submit --driver-memory 8G randomProjection.py
def random_project_map_function(feature_vector, local_csr_matrix):
    """
    ***NOTE***: This is a very inefficient method of doing matrix multiplication one row at a time. See it's faster
    cousin which operates on a whole partition at a time in random_project_mappartitions_function below

    This function is intended to be used in a <df>.rdd.map(lambda x: random_project_map_function(x, local_csr_matrix))
    setting

    :param feature_vector: this is a single n-dim sparse vector which need to be projected into a low-dim space
     specified by local_csr_matrix
    :param local_csr_matrix: the projection matrix - should have dimensions: n x p
    :return: a single p-dim sparse-vector.
    """
    data = feature_vector.values.astype(np.float32)
    col_indices = feature_vector.indices
    row_indices = [0] * len(col_indices)  # [0]* len of col indices because we are dealing with a matrix with 1 row and n cols
    feature_mat = ssp.csr_matrix((data, (row_indices, col_indices)), shape=(1, feature_vector.size))
    prod_result = feature_mat.dot(local_csr_matrix).tocoo()
    indices = prod_result.col
    values = prod_result.data.astype(np.float32)
    ndim = prod_result.shape[1]
    return Vectors.sparse(ndim, dict(zip(indices, values)))


def random_project_mappartitions_function(rdd_row_iterator, local_csr_matrix):
    """
    This function is intended to be used in a <df>.rdd.mapPartitions(lambda rdd_map: random_project_mapf(rdd_map, local_rnd_mat))
    setting.
    :param rdd_row_iterator: a list of n-dim sparse vectors
    :param local_csr_matrix: the projection matrix - should have dimensions: n x p
    :return: a list of p-dim sparse-vectors - same length as input
    """
    keys = []
    # this will be a list of single row sparsevectors transformed into scipy csr matrix
    features_single_row_matrix_list = []
    PROJECT_DIM_SIZE = local_csr_matrix.shape[1]

    for row in rdd_row_iterator:
        # capture keys
        if "label" in row:
            keys.append((row["id"], row["label"]))
        else:
            keys.append((row["id"]))
        # work on values:
        feature_dim_size = row["features"].size  # feature dimensionality before projection
        col_indices = row["features"].indices
        row_indices = [0] * len(col_indices)  # defaulting to 0 as we are creating single row matrix
        data = row["features"].values.astype(np.float32)

        feature_mat = ssp.coo_matrix((data, (row_indices, col_indices)), shape=(1, feature_dim_size)).tocsr()
        features_single_row_matrix_list.append(feature_mat)
    # vstacking single row matrices into one large sparse matrix
    features_matrix = ssp.vstack(features_single_row_matrix_list)
    del features_single_row_matrix_list

    projected_features = features_matrix.dot(local_csr_matrix)
    del features_matrix, local_csr_matrix

    projected_features_list = (Vectors.sparse(PROJECT_DIM_SIZE, zip(i.indices, i.data))
                               for i in projected_features)
    if "label" in row:
        return zip((i[0] for i in keys), (i[1] for i in keys), projected_features_list)
    else:
        return zip((i[0] for i in keys), projected_features_list)


if __name__ == '__main__':
    N_FEATURES=54686452 # these are the no. of features in the dataset we are goining to use in this app
    logging.basicConfig(format='%(asctime)s %(levelname)s:%(message)s', level=logging.INFO)

    spark = SparkSession \
        .builder \
        .appName("PySpark Random Projection Demo") \
        .getOrCreate()

    DATA_DIR = '/Users/sashi/IdeaProjects/RandomProjection/data'

    train = spark.read.format("libsvm").load(os.path.join(DATA_DIR, "kdd12.tr"), numFeatures=N_FEATURES)\
            .withColumn("id", f.monotonically_increasing_id())
    print(train.show())
    valid = spark.read.format("libsvm").load(os.path.join(DATA_DIR, "kdd12.val"), numFeatures=N_FEATURES) \
            .withColumn("id", f.monotonically_increasing_id())

    train_schema = StructType(
        [StructField("id", LongType(), False)
           ,StructField("label", FloatType(), False)
            , StructField("features", VectorUDT(), False)
            ]
    )

    # generating the random projection  matrix
    dummy_X_rows = train.count()
    dummy_X_cols = train.first()["features"].size
    dummy_X = ssp.csr_matrix((dummy_X_rows, dummy_X_cols), dtype=np.float32)  # the shape-only of this dummy

    # find the optimal (conservative estimate) no. of dimensions required according to the
    # johnson_lindenstrauss_min_dim function
    rproj_ndim = johnson_lindenstrauss_min_dim(dummy_X_rows, eps=0.2) # returns 4250

    try:
        srp = joblib.load(os.path.join(DATA_DIR,"srp_{0}.pkl".format(rproj_ndim)))
    except FileNotFoundError:
        srp = SparseRandomProjection(n_components=rproj_ndim, random_state=123)
        logging.info("Fitting Sparse Random Projection to have %d columns after projection " % rproj_ndim)
        srp.fit(dummy_X)
        logging.info("Saving the projection matrix to disk... ")
        joblib.dump(srp, os.path.join(DATA_DIR,"srp_{0}.pkl".format(rproj_ndim)))

    local_rnd_mat = srp.components_.T.astype(np.float32)

    logging.info("Applying random projection to  rdd map partitions")
    valid_projected_df = valid.rdd\
        .mapPartitions(lambda rdd_map_partition: random_project_mappartitions_function(rdd_map_partition, local_rnd_mat))\
        .toDF(train_schema)
    train_projected_df = train.rdd\
        .mapPartitions(lambda rdd_map_partition: random_project_mappartitions_function(rdd_map_partition, local_rnd_mat))\
        .toDF(train_schema)

    logging.info("Writing projected data to disk...")
    # valid_projected_df.write.mode("overwrite").parquet(os.path.join(DATA_DIR,"valid_features_random_projected.parquet"))
    train_projected_df.write.mode("overwrite").parquet(os.path.join(DATA_DIR,"train_features_random_projected.parquet"))

    logging.info("Sample rows from training set before projection...")
    print(train.show())
    logging.info("Sample rows from training set after projection...")
    print(spark.read.parquet(os.path.join(DATA_DIR,"train_features_random_projected.parquet")).show())

    spark.stop()