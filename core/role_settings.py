"""Validated, atomic next-run role preferences (separate from base YAML)."""
import json
import os
import threading
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / 'data' / 'role_settings.json'
LOCK = threading.Lock()
ALIASES = {'codex': 'openai', 'openai': 'openai', 'claude': 'claude',
           'anthropic': 'claude', 'google': 'antigravity', 'antigravity': 'antigravity'}
DOMAINS = {'openai': 'OPENAI', 'claude': 'ANTHROPIC', 'antigravity': 'GOOGLE'}

def three_ai() -> bool:
    """Antigravity on: three AIs for three roles. Off (antigravity.enabled: false): two AIs hold the three roles."""
    from core.env_utils import antigravity_enabled
    return antigravity_enabled()

def validate(roles):
    if not isinstance(roles, dict) or set(roles) != {'head', 'builder', 'reviewer'}:
        raise ValueError('HEAD, BUILDER, REVIEWER 세 역할을 모두 지정해야 합니다.')
    if any(v not in DOMAINS for v in roles.values()):
        raise ValueError('알 수 없는 AI입니다.')
    if three_ai():
        if len(set(roles.values())) != 3:
            raise ValueError('세 역할에는 서로 다른 AI를 지정해야 합니다.')
    else:
        if 'antigravity' in roles.values():
            raise ValueError('Antigravity가 꺼져 있습니다 (config의 antigravity.enabled). Claude와 Codex 중에서 고르세요.')
        if roles['builder'] == roles['reviewer']:
            raise ValueError('BUILDER와 REVIEWER는 서로 다른 AI여야 합니다.')
    return dict(roles)

def fit_roles(roles, three):
    """Roles that fit the new mode. Turning Antigravity on gives it the role that the AI holding two roles held as
    BUILDER (else HEAD); turning it off hands its role back so that BUILDER and REVIEWER stay different."""
    r = dict(roles)
    if three:
        if len(set(r.values())) == 3:
            return r
        if r['builder'] in (r['head'], r['reviewer']):
            r['builder'] = 'antigravity'
        elif r['head'] == r['reviewer']:
            r['head'] = 'antigravity'
        return r
    if r['builder'] == 'antigravity':
        r['builder'] = 'claude' if r['reviewer'] == 'openai' else 'openai'
    if r['reviewer'] == 'antigravity':
        r['reviewer'] = 'claude' if r['builder'] == 'openai' else 'openai'
    if r['head'] == 'antigravity':
        r['head'] = r['builder']
    return r


def set_antigravity(enabled: bool):
    """The console switch: write data/antigravity.json and move the saved roles so they are valid in the new mode.
    Returns the roles now saved. The switch file is restored when the roles cannot be saved."""
    from core import env_utils
    import director
    if not isinstance(enabled, bool):
        raise ValueError('enabled 값은 true 또는 false여야 합니다.')
    before = current(director.load_config())
    path = env_utils.ANTIGRAVITY_SWITCH
    old = path.read_text(encoding='utf-8') if path.exists() else None
    with LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps({'enabled': enabled}), encoding='utf-8')
        os.replace(temp, path)
    try:
        return save(fit_roles(before, enabled))
    except Exception:
        with LOCK:
            if old is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(old, encoding='utf-8')
        raise


def apply(config):
    if PATH.exists():
        try:
            roles = validate(json.loads(PATH.read_text(encoding='utf-8')))
        except (ValueError, OSError) as exc:
            # A saved choice that no longer fits (an Antigravity role after antigravity.enabled was turned off)
            # must not break every command: config.yaml's roles apply until a valid choice is saved again.
            import logging
            logging.getLogger(__name__).warning('role_settings.json ignored: %s', exc)
            return config

        for role, provider in roles.items():
            config.setdefault(role, {}).update(provider=provider, executable=None, model='cli-default')
        config['builder']['mode'] = 'proposal'
        config['manual_roles'] = True
    return config

def current(config):
    return {role: ALIASES[config[role]['provider'].lower()] for role in ('head', 'builder', 'reviewer')}

def save(roles):
    roles = validate(roles)
    with LOCK:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        temp = PATH.with_suffix('.tmp')
        temp.write_text(json.dumps(roles, ensure_ascii=False), encoding='utf-8')
        os.replace(temp, PATH)
    return roles
