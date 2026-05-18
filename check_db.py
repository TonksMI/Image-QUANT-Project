import psycopg2, os, sys
sys.path.insert(0, 'src')
from dotenv import load_dotenv
load_dotenv()
host = os.getenv('POSTGRES_HOST', 'localhost')
port = os.getenv('POSTGRES_PORT', '5432')
db   = os.getenv('POSTGRES_DB', 'urbangrowth')
user = os.getenv('POSTGRES_USER', 'urbangrowth')
pw   = os.getenv('POSTGRES_PASSWORD', 'changeme')
conn = psycopg2.connect(host=host, port=port, dbname=db, user=user, password=pw)
cur = conn.cursor()
cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
tables = [r[0] for r in cur.fetchall()]
for t in tables:
    cur.execute(f'SELECT COUNT(*) FROM {t}')
    print(f'{t}: {cur.fetchone()[0]}')
conn.close()
