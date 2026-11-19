"""Entry point kompatibilitas lama; implementasi berada di Sources/ (Sources/main.py).

Kontrak shim yang dijamin: `python server.py` menjalankan server (sama seperti
`python -m Sources.main`), dan nama `AlertServer` tetap bisa diimpor dari sini
sebagai alias Sources.alerts.server.AlertServer. Nama lain tidak dijanjikan.
"""
from Sources.alerts.server import AlertServer
from Sources.main import main


if __name__ == "__main__":
    main()
