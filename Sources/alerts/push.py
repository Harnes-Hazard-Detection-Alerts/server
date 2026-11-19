"""APNs provider used for safety alerts.

The default ``mock`` mode never opens a network connection. ``prod`` uses the
HTTP/2 APNs provider API and an ES256 provider token backed by a ``.p8`` key.
The key must stay outside this repository and is loaded from ``APNS_KEY_PATH``.
"""
import json
import threading
import time
from pathlib import Path

from ..config import env_value


_TOKEN_REFRESH_SECONDS = 50 * 60
_APNS_HOSTS = {
    'development': 'api.sandbox.push.apple.com',
    'production': 'api.push.apple.com',
}


class PushConfigurationError(ValueError):
    """Raised when production APNs configuration is missing or unsafe."""


class PushSender:
    """Send APNs alerts with a reusable HTTP/2 client and cached provider JWT."""

    def __init__(self, mode=None, *, client=None, clock=None, token_encoder=None,
                 key_loader=None):
        self.mode = (mode or env_value('APNS_MODE') or 'mock').lower()
        if self.mode not in ('mock', 'prod'):
            raise PushConfigurationError('APNS_MODE must be mock or prod')

        self._client = client
        self._clock = clock or time.time
        self._token_encoder = token_encoder
        self._key_loader = key_loader or self._read_private_key
        self._private_key = None
        self._provider_token = None
        self._provider_token_issued_at = None
        self._token_lock = threading.Lock()
        self._client_lock = threading.Lock()
        self._closed = False

        if self.mode == 'prod':
            self.key_path = self._required_env('APNS_KEY_PATH')
            self.key_id = self._required_env('APNS_KEY_ID')
            self.team_id = self._required_env('APNS_TEAM_ID')
            self.bundle_id = self._required_env('APNS_BUNDLE_ID')
            self.environment = (env_value('APNS_ENVIRONMENT') or 'development').lower()
            if self.environment not in _APNS_HOSTS:
                raise PushConfigurationError(
                    'APNS_ENVIRONMENT must be development or production')
            self.host = _APNS_HOSTS[self.environment]

    @staticmethod
    def _required_env(name):
        value = env_value(name)
        if value is None or not value.strip():
            raise PushConfigurationError(f'{name} is required when APNS_MODE=prod')
        return value.strip()

    @staticmethod
    def _read_private_key(path_text):
        path = Path(path_text).expanduser().resolve()
        server_root = Path(__file__).resolve().parents[2]
        if path == server_root or server_root in path.parents:
            raise PushConfigurationError('APNS_KEY_PATH must point outside AppleApp/Server')
        try:
            return path.read_text(encoding='utf-8')
        except OSError as error:
            raise PushConfigurationError('APNS_KEY_PATH cannot be read') from error

    @staticmethod
    def _encode_with_pyjwt(payload, key, *, algorithm, headers):
        try:
            import jwt
        except ImportError as error:
            raise PushConfigurationError(
                'APNs prod requires PyJWT[crypto] from requirements.txt') from error
        return jwt.encode(payload, key, algorithm=algorithm, headers=headers)

    def _http_client(self):
        with self._client_lock:
            if self._client is None:
                try:
                    import httpx
                except ImportError as error:
                    raise PushConfigurationError(
                        'APNs prod requires httpx[http2] from requirements.txt') from error
                self._client = httpx.Client(http2=True, timeout=10.0)
            return self._client

    def _authorization_token(self):
        now = int(self._clock())
        with self._token_lock:
            if (self._provider_token is not None
                    and now - self._provider_token_issued_at < _TOKEN_REFRESH_SECONDS):
                return self._provider_token
            if self._private_key is None:
                self._private_key = self._key_loader(self.key_path)
            encoder = self._token_encoder or self._encode_with_pyjwt
            self._provider_token = encoder(
                {'iss': self.team_id, 'iat': now}, self._private_key,
                algorithm='ES256', headers={'kid': self.key_id})
            self._provider_token_issued_at = now
            return self._provider_token

    # Custom loud danger sound bundled in the iOS app
    # (Apps/iOS/Resources/harnes_alert.caf; < 30 s, IMA4/caf per APNs rules).
    # Zone clear stays on the calm default tone.
    DANGER_SOUND = 'harnes_alert.caf'

    @staticmethod
    def _payload(alert):
        is_clear = alert.get('kind') == 'zone_cleared'
        return {
            'aps': {
                'alert': {
                    'title': 'Zone Clear' if is_clear else 'New safety alert',
                    'body': ('No worker detected in zone' if is_clear else
                             'Worker detected in hazard zone. Stop operating immediately. '
                             'Keep the excavator stopped until the hazard is resolved '
                             'and the area is confirmed clear.'),
                },
                'sound': 'default' if is_clear else PushSender.DANGER_SOUND,
                'badge': 1,
                # Remote alerts intentionally expose no local-only action.
                # UNIT_STOPPED depends on a local Incident ID and must not be
                # shown for a server alert that cannot acknowledge that object.
                'category': 'ZONE_CLEAR' if is_clear else 'REMOTE_DANGER',
                'interruption-level': 'active' if is_clear else 'time-sensitive',
            },
            'kind': alert.get('kind'),
            'equipmentInstanceId': alert.get('equipmentInstanceId'),
            'personsInZone': alert.get('personsInZone', 0),
            'serverAlertId': alert.get('id'),
        }

    def send(self, push_token, alert, bundle_id=None):
        """Send one alert and return a structured APNs result.

        ``ok`` is true only for an APNs HTTP 200 response (or explicitly marked
        ``mock`` output). No private key, provider token, or full device token is
        included in the returned value or production logs.
        """
        payload = self._payload(alert)
        if self.mode == 'mock':
            encoded = json.dumps(payload, default=str)
            print(f'[push/mock] would send to {push_token[:8]}…: '
                  f'{alert.get("kind")} {encoded}', flush=True)
            return {'ok': True, 'mock': True, 'status': None,
                    'reason': None, 'apns_id': None}
        if self._closed:
            return {'ok': False, 'status': None, 'reason': 'sender_closed',
                    'apns_id': None}

        headers = {
            'authorization': f'bearer {self._authorization_token()}',
            'apns-topic': bundle_id or self.bundle_id,
            'apns-push-type': 'alert',
            'apns-priority': '10',
            # Time-sensitive: kirim sekarang atau buang — APNs tidak menyimpan
            # (epoch 0). Alert bahaya telat = noise berbahaya (test_push specced).
            'apns-expiration': '0',
        }
        try:
            response = self._http_client().post(
                f'https://{self.host}/3/device/{push_token}',
                headers=headers, json=payload)
        except Exception as error:
            return {'ok': False, 'status': None,
                    'reason': type(error).__name__, 'apns_id': None}

        reason = None
        if response.status_code != 200:
            try:
                body = response.json()
                reason = body.get('reason') if isinstance(body, dict) else None
            except (ValueError, TypeError):
                reason = None
            reason = reason or f'HTTP_{response.status_code}'
        apns_id = response.headers.get('apns-id')
        return {'ok': response.status_code == 200,
                'status': response.status_code,
                'reason': reason,
                'apns_id': apns_id}

    def close(self):
        """Close the reusable APNs HTTP/2 connection."""
        with self._client_lock:
            if self._closed:
                return
            self._closed = True
            if self._client is not None:
                self._client.close()

    def resolve_tokens(self, operator_id, connection_factory):
        """Query device_tokens for operator, return list of (device_id, push_token)."""
        with connection_factory() as connection:
            return connection.execute(
                'SELECT id, push_token FROM device_tokens WHERE operator_id=%s',
                (operator_id,)).fetchall()

    def resolve_binding(self, equipment_instance_id, connection_factory):
        """Query shift_bindings for equipment, return list of operator_id."""
        with connection_factory() as connection:
            return [str(r[0]) for r in connection.execute(
                'SELECT operator_id FROM shift_bindings WHERE equipment_instance_id=%s',
                (equipment_instance_id,)).fetchall()]
