"""Seed default admin + sample stores."""
import os
import uuid
from sqlalchemy.orm import Session
from models import HQUser, Store
from auth import hash_password, verify_password


SAMPLE_STORES = [
    # code, name, city, region, is_online
    ("MUM-001", "Mumbai Andheri Flagship", "Mumbai", "West", False),
    ("MUM-002", "Mumbai Bandra Outlet", "Mumbai", "West", False),
    ("PUN-001", "Pune Koregaon Park", "Pune", "West", False),
    ("DEL-001", "Delhi Connaught Place", "Delhi", "North", False),
    ("DEL-002", "Delhi Saket Mall", "Delhi", "North", False),
    ("GUR-001", "Gurgaon Cyber Hub", "Gurgaon", "North", False),
    ("BLR-001", "Bangalore MG Road", "Bangalore", "South", False),
    ("BLR-002", "Bangalore Whitefield", "Bangalore", "South", False),
    ("HYD-001", "Hyderabad Banjara Hills", "Hyderabad", "South", False),
    ("CHN-001", "Chennai T. Nagar", "Chennai", "South", False),
    ("KOL-001", "Kolkata Park Street", "Kolkata", "East", False),
    ("ONL-001", "Online Store - Web", "Online", "Online", True),
    ("ONL-002", "Online Store - App", "Online", "Online", True),
]


def seed_admin(db: Session):
    username = os.environ.get("ADMIN_USERNAME", "admin")
    password = os.environ.get("ADMIN_PASSWORD", "admin123")
    existing = db.query(HQUser).filter(HQUser.username == username).first()
    if existing is None:
        db.add(HQUser(username=username, password_hash=hash_password(password), role="admin"))
        db.commit()
    else:
        # idempotent: keep hash aligned with env password
        if not verify_password(password, existing.password_hash):
            existing.password_hash = hash_password(password)
            db.commit()


def seed_stores(db: Session):
    if db.query(Store).count() > 0:
        return
    for code, name, city, region, is_online in SAMPLE_STORES:
        db.add(Store(
            store_code=code,
            store_name=name,
            city=city,
            region=region,
            is_online_store=is_online,
            receiver_token=uuid.uuid4().hex,
        ))
    db.commit()
