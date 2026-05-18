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

queries = {
    'census_bps': "SELECT MIN(period), MAX(period), COUNT(DISTINCT period) FROM census_bps",
    'fred_series': "SELECT MIN(date), MAX(date), COUNT(DISTINCT series_id) FROM fred_series",
    'prices': "SELECT MIN(date), MAX(date), COUNT(DISTINCT symbol) FROM prices",
    'returns': "SELECT MIN(date), MAX(date), COUNT(DISTINCT symbol) FROM returns",
    'signal_features': "SELECT MIN(period), MAX(period), COUNT(DISTINCT feature_name) FROM signal_features",
    'h3_features': "SELECT MIN(date), MAX(date), COUNT(*) FROM h3_features",
}
for name, q in queries.items():
    cur.execute(q)
    row = cur.fetchone()
    print(f'{name}: min={row[0]} max={row[1]} count={row[2]}')
conn.close()
