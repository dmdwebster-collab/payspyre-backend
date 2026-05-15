from uuid import uuid4
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.security import get_password_hash
from app.db.base import Base
from app.models.user import User, Role, UserRoleLink


def seed_admin_user():
    engine = create_engine(settings.DATABASE_URL)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()

    try:
        admin_role = db.query(Role).filter(Role.name == "admin").first()
        if not admin_role:
            admin_role = Role(
                name="admin",
                description="System administrator with full access",
                is_system=True
            )
            db.add(admin_role)
            db.commit()
            db.refresh(admin_role)

        existing_admin = db.query(User).filter(User.email == "admin@payspyre.com").first()
        if existing_admin:
            print("Admin user already exists")
            return

        admin_user = User(
            email="admin@payspyre.com",
            password_hash=get_password_hash("Admin123!ChangeMe"),
            first_name="System",
            last_name="Administrator",
            phone="+18005550000",
            is_active=True,
            is_verified=True,
        )
        db.add(admin_user)
        db.commit()
        db.refresh(admin_user)

        user_role = UserRoleLink(user_id=admin_user.id, role_id=admin_role.id)
        db.add(user_role)
        db.commit()

        print("Admin user created successfully")
        print(f"Email: admin@payspyre.com")
        print(f"Password: Admin123!ChangeMe")
        print("Please change the password after first login")

    except Exception as e:
        db.rollback()
        print(f"Error seeding admin user: {e}")
    finally:
        db.close()


if __name__ == "__main__":
    seed_admin_user()