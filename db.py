import os
from psycopg2.pool import SimpleConnectionPool

DB_POOL = SimpleConnectionPool(
    minconn=1,
    maxconn=10,
    dbname=os.getenv("DB_NAME"),
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
    host=os.getenv("DB_HOST"),
    port=int(os.getenv("DB_PORT"))
)

def get_conn():
    return DB_POOL.getconn()

def put_conn(conn):
    DB_POOL.putconn(conn)
