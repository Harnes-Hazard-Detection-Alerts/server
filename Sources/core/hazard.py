"""Domain murni — state machine histeresis waktu untuk status bahaya.

Port setia dari workspace HeavyMachineZone-Model/vision/pipeline.py versi fix
7 Nov 2026 (zero-order hold): durasi dihitung dari dt NYATA antar sampel —
interval [t_i, t_{i+1}] milik status sampel i, dan sampel TERAKHIR belum punya
interval. Jadi satu frame tunggal TIDAK PERNAH langsung CONFIRM/CLEAR
(anti-flap; bug lama dt = WINDOW/n yang membuat frame pertama dihitung satu
detik penuh — instant-CONFIRM — tidak mungkin kambuh secara struktural).

Status: clear → potential → confirmed; confirmed → clear (butuh waktu di luar).
Parameter tersuntik (semua satuan detik):
    CONFIRM_TIME_S = 0.25  — akumulatif dalam zona untuk CONFIRM
    CLEAR_TIME_S   = 0.5   — akumulatif luar zona untuk CLEAR
    WINDOW_S       = 1.0   — voting window akumulasi
    clear_hold_s   — opsional: CLEAR juga butuh tidak ada sampel dalam zona
                     selama clear_hold_s detik BERUNTUN (anti kedip deteksi;
                     keputusan owner 12 Nov 2026: server memakai CLEAR_HOLD_S).
                     Default konstruktor 0 = perilaku lama (hanya clear_time_s).

Modul stdlib saja (aturan core clean-lite).
"""

CONFIRM_TIME_S = 0.25
CLEAR_TIME_S = 0.5
WINDOW_S = 1.0
# Lama zona harus kosong berturut-turut sebelum bahaya dinyatakan selesai
# (dipakai server lewat --clear-hold / env CLEAR_HOLD_S).
CLEAR_HOLD_S = 3.0

CONFIRMED = 'confirmed'
CLEARED = 'cleared'


class HazardState:
    """Voting window + histeresis waktu; satu unit (source/equipment) = satu instance."""

    def __init__(self, confirm_time_s=CONFIRM_TIME_S, clear_time_s=CLEAR_TIME_S,
                 window_s=WINDOW_S, clear_hold_s=0.0):
        if confirm_time_s < 0 or clear_time_s < 0 or window_s <= 0 or clear_hold_s < 0:
            raise ValueError('parameter waktu hazard harus >= 0 dan window > 0')
        self.confirm_time_s = confirm_time_s
        self.clear_time_s = clear_time_s
        self.window_s = window_s
        self.clear_hold_s = clear_hold_s
        self.status = 'clear'  # clear | potential | confirmed
        self._samples = []     # (t_detik, in_zone) — urut waktu
        self._last_in_t = None  # waktu sampel dalam-zona terakhir (untuk clear_hold_s)

    def update(self, in_zone, t_seconds):
        """Catat satu sampel; balik 'confirmed'/'cleared' saat transisi, lainnya None."""
        if in_zone:
            self._last_in_t = t_seconds
        self._samples.append((t_seconds, bool(in_zone)))
        cutoff = t_seconds - self.window_s
        self._samples = [(ts, zone) for ts, zone in self._samples if ts >= cutoff]

        time_in = 0.0
        time_out = 0.0
        previous = None
        for ts, zone in self._samples:
            if previous is not None:
                dt = ts - previous[0]
                if dt > self.window_s:
                    dt = self.window_s  # gap besar (lompat waktu/stride) — jangan overcount
                if dt > 0:
                    if previous[1]:
                        time_in += dt
                    else:
                        time_out += dt
            previous = (ts, zone)

        event = None
        if self.status in ('clear', 'potential'):
            if in_zone and time_in >= self.confirm_time_s:
                self.status = 'confirmed'
                event = CONFIRMED
            elif in_zone:
                self.status = 'potential'
            else:
                self.status = 'clear'
        elif self.status == 'confirmed':
            held = (self.clear_hold_s <= 0 or self._last_in_t is None
                    or t_seconds - self._last_in_t >= self.clear_hold_s)
            if not in_zone and time_out >= self.clear_time_s and held:
                self.status = 'clear'
                event = CLEARED
            # masih within clear-time tapi pernah keluar — tetap confirmed (histeresis)
        return event

    def rebase(self, t_seconds, in_zone=None):
        """Discard elapsed voting time while observation was unavailable."""
        sample = self.status == 'confirmed' if in_zone is None else bool(in_zone)
        self._samples = [(t_seconds, sample)]
        if sample or self.status == CONFIRMED:
            # Missing observations cannot count toward consecutive clear time,
            # even when the first recovered frame contains no worker.
            self._last_in_t = t_seconds
