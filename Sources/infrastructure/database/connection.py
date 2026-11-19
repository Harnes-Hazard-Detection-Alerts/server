"""Local database configuration without logging credentials.

Pembacaan .env dipusatkan di Sources/config (pemilik tunggal SERVER_ROOT/ENV_FILE);
modul ini hanya memvalidasi dan memakai DATABASE_URL.
"""
import psycopg
from psycopg.conninfo import conninfo_to_dict

from ...config import env_values


def database_url():
    values = env_values('DATABASE_URL')
    if not values:
        raise ValueError('DATABASE_URL belum tersedia. Jalankan setup-database.sh.')
    if len(values) != 1:
        raise ValueError('Isi tepat satu DATABASE_URL di Server/.env.')
    value = values[0]
    try:
        options = conninfo_to_dict(value)
    except psycopg.Error:
        raise ValueError('Format DATABASE_URL tidak valid.') from None
    if not all(options.get(key) for key in ('dbname', 'user', 'password', 'host')):
        raise ValueError('DATABASE_URL harus berisi database, user, password, dan host.')
    return value


def connect(url=None):
    return psycopg.connect(url or database_url(), connect_timeout=5)


def main():
    try:
        with connect() as connection:
            connection.execute('SELECT 1').fetchone()
        print('Koneksi database berhasil.')
    except (ValueError, psycopg.Error):
        raise SystemExit('Koneksi gagal. Periksa layanan PostgreSQL dan konfigurasi lokal; kredensial tidak ditampilkan.') from None


if __name__ == '__main__':
    main()
