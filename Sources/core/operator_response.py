"""Domain murni — jawaban operator saat bahaya bertahan (api-draft 4.7b).

Keputusan owner 12 Nov 2026: bila bahaya masih ada setelah 10 detik, operator
memilih satu alasan. Jawaban adalah CATATAN untuk HSE; tidak menutup alert
(`resolved` tetap dari evaluasi kamera). Stdlib saja (aturan core clean-lite);
dipakai alerts/store (validasi + error) dan api/handlers (pemetaan HTTP).
"""

OPERATOR_RESPONSES = ('worker_needed', 'worker_moving_away', 'false_detection',
                      'needs_hse_help')

# Event typed WebSocket/SSE yang memberi tahu HSE (payload: equipmentInstanceId,
# alertIds, response, respondedAtMs, operatorId, operatorName).
OPERATOR_RESPONSE_EVENT = 'alert.operator_response'
HAZARD_ACTIVE_EVENT = 'alert.hazard_active'
HSE_HAZARD_ACTION_EVENT = 'alert.hse_action'


class OperatorResponseError(Exception):
    """Jawaban ditolak: invalid_response | no_active_hazard | already_responded."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code
