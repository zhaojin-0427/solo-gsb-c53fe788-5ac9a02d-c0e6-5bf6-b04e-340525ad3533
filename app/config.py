import os

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://mail:mail@127.0.0.1:5432/mailmerge"
)
