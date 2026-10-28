# -*- coding: utf-8 -*-
# 合并重复订阅：同 (user_id, plan_id) 的多条 active 行 -> 保留一行，剩余天数求和
from datetime import datetime, timedelta
from app import create_app, db
from app.models import Subscription
app = create_app()
with app.app_context():
    now = datetime.utcnow()
    groups = {}
    for s in Subscription.query.filter_by(status='active').all():
        groups.setdefault((s.user_id, s.plan_id), []).append(s)
    merged = 0; deleted = 0
    for key, rows in groups.items():
        if len(rows) < 2: continue
        total = 0.0
        for s in rows:
            if s.expires_at and s.expires_at > now:
                total += (s.expires_at - now).total_seconds() / 86400.0
        keep = sorted(rows, key=lambda x: (x.started_at or now))[0]
        keep.expires_at = now + timedelta(days=total)
        keep.status = 'active'
        for s in rows:
            if s is not keep:
                db.session.delete(s); deleted += 1
        merged += 1
    db.session.commit()
    print('merged groups: %d, deleted rows: %d' % (merged, deleted))
    left = Subscription.query.filter_by(status='active').count()
    print('active rows now:', left)