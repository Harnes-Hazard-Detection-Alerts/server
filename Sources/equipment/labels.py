"""Profil excavator yang diatur HSE (api-draft 4.4b): nama tampilan dan
nomor telepon iPhone kabin.

Nama dan nomor hanya data tampilan — identitas & routing tetap memakai
equipmentInstanceId (CONTEXT P3). HSE menelepon kabin excavator, bukan
nomor pribadi operator (keputusan owner 11 Nov 2026).
"""
from ..infrastructure.database.connection import connect

FIELDS = ('name', 'cabinPhone')


def _profile(row):
    return {'name': row[0], 'cabinPhone': row[1]}


def list_profiles(connection_factory=connect):
    """{equipmentInstanceId: {name, cabinPhone}} untuk semua unit yang diatur."""
    with connection_factory() as connection:
        rows = connection.execute(
            'SELECT equipment_instance_id, display_name, cabin_phone '
            'FROM equipment_labels').fetchall()
    return {row[0]: _profile(row[1:]) for row in rows}


def get_profile(equipment_instance_id, connection_factory=connect):
    with connection_factory() as connection:
        row = connection.execute(
            'SELECT display_name, cabin_phone FROM equipment_labels '
            'WHERE equipment_instance_id=%s', (equipment_instance_id,)).fetchone()
    return _profile(row) if row else {'name': None, 'cabinPhone': None}


def get_label(equipment_instance_id, connection_factory=connect):
    return get_profile(equipment_instance_id, connection_factory=connection_factory)['name']


def update_profile(equipment_instance_id, changes, user_id, now_ms,
                   connection_factory=connect):
    """Ubah sebagian profil. `changes` hanya berisi kunci yang dikirim;
    nilai None menghapus kolom itu. Profil kosong total dihapus."""
    current = get_profile(equipment_instance_id, connection_factory=connection_factory)
    merged = {**current, **{key: changes[key] for key in FIELDS if key in changes}}
    with connection_factory() as connection:
        if merged['name'] is None and merged['cabinPhone'] is None:
            connection.execute('DELETE FROM equipment_labels WHERE equipment_instance_id=%s',
                               (equipment_instance_id,))
            return merged
        connection.execute(
            'INSERT INTO equipment_labels(equipment_instance_id, display_name, cabin_phone, '
            'updated_at_ms, updated_by) VALUES (%s, %s, %s, %s, %s) '
            'ON CONFLICT (equipment_instance_id) DO UPDATE SET '
            'display_name=EXCLUDED.display_name, cabin_phone=EXCLUDED.cabin_phone, '
            'updated_at_ms=EXCLUDED.updated_at_ms, updated_by=EXCLUDED.updated_by',
            (equipment_instance_id, merged['name'], merged['cabinPhone'], now_ms, user_id))
        if merged['name'] is not None:
            # Jembatan katalog (migrasi 013/014, FK assignments): nama katalog
            # ikut label supaya listing assignment tidak menampilkan nama basi.
            # Registry/labels tetap sumber kebenaran — katalog hanya salinan.
            connection.execute(
                'INSERT INTO equipment_instances(equipment_instance_id, name) '
                'VALUES (%s, %s) ON CONFLICT (equipment_instance_id) '
                'DO UPDATE SET name=EXCLUDED.name',
                (equipment_instance_id, merged['name']))
    return merged
