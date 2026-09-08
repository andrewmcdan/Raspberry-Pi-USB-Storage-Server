"""Retention worker; activation scheduling is evaluated transactionally at check-in."""
import time
from pathlib import Path
from sqlalchemy import select, text
from manager.app import create_app
from manager.models import PiSnapshot, Collection, Deployment, Device, Review


def collect(app, now=None):
    now = time.time() if now is None else now
    with app.extensions['db']() as db:
        if db.bind.dialect.name == 'postgresql':
            db.execute(text('SELECT pg_advisory_xact_lock(731952)'))
        protected = set()
        # Match API lock ordering and prevent assigning a draft while collecting its content.
        devices = list(db.scalars(select(Device).order_by(Device.id).with_for_update()))
        collections = list(db.scalars(select(Collection).with_for_update()))
        for device in devices:
            protected.update(filter(None, (device.telemetry.get('active_deployment'),
                                          device.telemetry.get('prepared_deployment'))))
            rows = list(db.scalars(select(Deployment).where(Deployment.device_id == device.id)
                                  .order_by(Deployment.created.desc()).with_for_update()))
            retained = [x for x in rows if x.manifest is not None]
            protected.update(x.id for x in retained[:10])
            for row in rows:
                if row.pinned or row.state not in ('succeeded', 'failed', 'canceled'):
                    protected.add(row.id)
                if row.id not in protected:
                    row.manifest = None
        refs = set()
        manifests = [x.draft for x in devices + collections]
        manifests += [x.manifest for x in db.scalars(select(Deployment)) if x.manifest is not None]
        manifests += [x.manifest for x in db.scalars(select(PiSnapshot))]
        for review in db.scalars(select(Review).with_for_update()):
            if now - review.created < 3600:
                manifests += [x['manifest'] for x in review.payload['entries']]
            elif review.payload.get('entries'):
                # Keep submission identity for idempotence, discard expired snapshot copies.
                review.payload = {'entries': []}
        for manifest in manifests:
            refs.update(x['sha256'] for x in manifest if x['kind'] == 'file')
        # A 24-hour grace protects uploads not yet attached to a draft.
        for path in Path(app.config['CONTENT_DIR']).iterdir():
            if path.name not in refs and now - path.stat().st_mtime > 86400 and path.is_file():
                path.unlink()
        db.commit()


if __name__ == '__main__':
    app = create_app()
    while True:
        try:
            collect(app)
        except Exception:
            app.logger.exception('Retention pass failed')
        time.sleep(60)
