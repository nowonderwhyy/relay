"""Flask web application factory and HTTP API for Relay."""
from __future__ import annotations

import datetime
import json
import secrets
import shutil
import time
from pathlib import PurePosixPath
from typing import Any

from .config import SPEED_LIMITS, ConfigStore, Settings, static_dir
from .core import (
    MAX_TORRENT,
    Relay,
    UserError,
    check_password,
    clean_name,
    disposition,
    effective_client_ip,
    is_allowed_peer,
    is_page_host,
    password_stamp,
    resolve_inside,
    same_person,
    validate_link,
    zip_stream,
)
from .engine import EngineDown, RPCError


def create_app(relay: Relay, config: ConfigStore, settings: Settings):
    from flask import Flask, Response, abort, jsonify, request, send_file, session

    static_path = static_dir()
    app = Flask(__name__, static_folder=str(static_path), static_url_path='/static')
    app.config.update(
        SECRET_KEY=config.get().get('secret_key') or secrets.token_hex(32),
        SESSION_COOKIE_NAME='relay_session',
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Strict',
        PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=90),
        MAX_CONTENT_LENGTH=MAX_TORRENT + 1024 * 1024,
    )
    failures: dict[str, tuple[int, float]] = {}
    open_paths = {'/', '/healthz', '/api/me', '/api/login'}

    def signed_in() -> bool:
        stored = config.get().get('password_hash', '')
        return bool(session.get('name') and session.get('stamp') == password_stamp(stored))

    def mine(job: dict[str, Any]) -> bool:
        return same_person(job['owner'], session.get('name'))

    def current_job(job_id: str) -> dict[str, Any]:
        job = relay.store.get(job_id)
        if not job or not mine(job):
            abort(404)
        return job

    def client_ip() -> str:
        return effective_client_ip(
            request.remote_addr,
            request.headers.get('X-Forwarded-For'),
            settings.trusted_proxies,
        )

    @app.before_request
    def gate():
        if not is_allowed_peer(
            client_ip(),
            allow_cgnat=settings.allow_cgnat,
            allow_public_clients=settings.allow_public_clients,
            allowed_networks=settings.allowed_networks,
        ):
            abort(403)
        if not is_page_host(request.host, settings.allowed_hosts):
            abort(403)
        if request.method == 'POST' and request.headers.get('X-Relay') != '1':
            abort(403)
        if request.path not in open_paths and not request.path.startswith('/static/') and not signed_in():
            return jsonify(error='Sign in first.'), 401

    @app.errorhandler(UserError)
    def user_error(exc):
        return jsonify(error=str(exc)), 400

    @app.errorhandler(EngineDown)
    def engine_down(exc):
        return jsonify(error=str(exc)), 503

    @app.errorhandler(RPCError)
    def rpc_error(exc):
        return jsonify(error='The download engine said: ' + str(exc)), 502

    @app.get('/')
    def index():
        response = send_file(static_path / 'index.html', max_age=0)
        response.headers['Cache-Control'] = 'no-cache'
        return response

    @app.get('/healthz')
    def healthz():
        health = relay.engine.health
        return jsonify(
            ok=True,
            engine=bool(relay.engine.token),
            vpn_mode=settings.vpn_mode,
            vpn=bool(health.get('vpn_ready')),
        )

    @app.get('/api/me')
    def me():
        return jsonify(
            signed_in=signed_in(),
            name=session.get('name', ''),
            needs_password=bool(config.get().get('password_hash')),
            instance_name=settings.instance_name,
            host_label=settings.effective_host_label,
            vpn_mode=settings.vpn_mode,
        )

    @app.post('/api/login')
    def login():
        stored = config.get().get('password_hash', '')
        data = request.get_json(silent=True) or {}
        name = clean_name(data.get('name'))
        if stored:
            address = client_ip()
            count, until = failures.get(address, (0, 0.0))
            if time.time() < until:
                raise UserError(f'Too many tries. Wait {int(until - time.time()) + 1} seconds.')
            if not check_password(str(data.get('password', '')), stored):
                count += 1
                failures[address] = (count, time.time() + (30 if count >= 5 else 0))
                raise UserError("That password isn't right.")
            failures.pop(address, None)
        session.clear()
        session.permanent = True
        session.update(name=name, stamp=password_stamp(stored))
        return jsonify(ok=True, name=name)

    @app.post('/api/logout')
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.get('/api/status')
    def status():
        settings.staging_dir.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(settings.staging_dir)
        health = relay.engine.health
        return jsonify(
            me=session['name'],
            instance_name=settings.instance_name,
            host_label=settings.effective_host_label,
            vpn_mode=settings.vpn_mode,
            vpn=bool(health.get('vpn_ready')),
            vpn_interface=health.get('vpn_interface') or settings.vpn_interface,
            vpn_guarded=bool(health.get('guard')),
            forward_port=relay.engine.forward,
            engine=bool(relay.engine.token),
            message=relay.engine.message,
            disk={'free': usage.free, 'total': usage.total},
            keep_days=settings.keep_days,
            now=time.time(),
            speed_limit=relay.store.setting('speed_limit', '0'),
            speed_limits=SPEED_LIMITS,
            jobs=[relay.public(job) for job in relay.store.all() if mine(job)],
        )

    @app.post('/api/jobs')
    def add_job():
        owner = session['name']
        upload = request.files.get('torrent')
        if upload:
            torrent = upload.read(MAX_TORRENT + 1)
            if len(torrent) > MAX_TORRENT:
                raise UserError('Torrent files over 10 MB are not supported.')
            if not torrent.startswith(b'd') or b'4:info' not in torrent:
                raise UserError("That file doesn't look like a .torrent file.")
            name = PurePosixPath((upload.filename or 'Torrent').replace('\\', '/')).stem[:200] or 'Torrent'
            job_id = relay.add(owner, 'torrent', upload.filename or 'upload.torrent', name, torrent)
        else:
            link, kind, name = validate_link(
                (request.get_json(silent=True) or {}).get('link'),
                allow_local_links=settings.allow_local_links,
            )
            job_id = relay.add(owner, kind, link, name)
        return jsonify(ok=True, id=job_id)

    @app.post('/api/jobs/<job_id>/<action>')
    def job_action(job_id: str, action: str):
        current_job(job_id)
        relay.act(job_id, action)
        return jsonify(ok=True)

    @app.post('/api/clear')
    def clear():
        cleared = relay.clear_history([job['id'] for job in relay.store.all() if mine(job)])
        return jsonify(ok=True, cleared=cleared)

    @app.post('/api/settings')
    def update_settings():
        limit = (request.get_json(silent=True) or {}).get('speed_limit')
        if limit not in SPEED_LIMITS:
            raise UserError('Pick one of the listed speed limits.')
        relay.store.set_setting('speed_limit', limit)
        relay.apply_speed_limit()
        return jsonify(ok=True)

    @app.get('/api/location')
    def location():
        data = relay.engine.locations_list()
        labels = settings.location_labels
        rows = []
        for row in data.get('locations', []):
            slug = row.get('slug', '')
            if not slug:
                continue
            rows.append({
                'slug': slug,
                'label': labels.get(slug) or slug.replace('-', ' ').title(),
                'endpoint': row.get('endpoint', ''),
                'active': bool(row.get('active')),
            })
        return jsonify(
            ok=True,
            available=bool(data.get('available')) and len(rows) > 0,
            active=data.get('active'),
            locations=rows,
        )

    @app.post('/api/location')
    def switch_location():
        slug = str((request.get_json(silent=True) or {}).get('slug', '')).strip()
        data = relay.engine.locations_list()
        known = {row['slug'] for row in data.get('locations', []) if row.get('slug')}
        if not known:
            raise UserError('VPN exit switching is not configured.')
        if slug not in known:
            raise UserError('Pick one of the listed VPN exits.')
        if slug == data.get('active'):
            return jsonify(ok=True, active=slug, unchanged=True)
        relay.switching = True
        try:
            result = relay.engine.location_set(slug)
        finally:
            relay.switching = False
        relay.engine.token = None
        relay.engine.health = {'vpn_ready': bool(result.get('vpn_ready')), 'vpn_mode': settings.vpn_mode}
        relay.engine.forward = result.get('forward')
        relay.engine.message = ''
        return jsonify(
            ok=True,
            active=result.get('switched'),
            forward=result.get('forward'),
            vpn_ready=bool(result.get('vpn_ready')),
        )

    @app.get('/files/<job_id>/<path:relative>')
    def download_file(job_id: str, relative: str):
        job = current_job(job_id)
        if job['status'] != 'complete':
            raise UserError('That download is not ready to save.')
        full = resolve_inside(settings.staging_dir / job_id, relative)
        return send_file(
            full,
            as_attachment=True,
            download_name=PurePosixPath(relative).name,
            conditional=True,
            max_age=0,
        )

    @app.get('/zip/<job_id>')
    def download_zip(job_id: str):
        job = current_job(job_id)
        files = json.loads(job['files'] or '[]')
        if job['status'] != 'complete' or not files:
            raise UserError('That download is not ready to save.')
        return Response(
            zip_stream(settings.staging_dir / job_id, files),
            mimetype='application/zip',
            headers={
                'Content-Disposition': disposition(job['name'] + '.zip'),
                'Cache-Control': 'no-store',
            },
        )

    return app
