import os

from alembic import context
from sqlalchemy import create_engine

url = os.environ["DATABASE_URL"]
with create_engine(url).connect() as connection:
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()
