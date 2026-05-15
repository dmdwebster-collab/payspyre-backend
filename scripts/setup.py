import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "payspyre.db"

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

# Create tables
cursor.execute("""
    CREATE TABLE IF NOT EXISTS loan_applications (
        id TEXT PRIMARY KEY,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
""")

cursor.execute("""
    CREATE TABLE IF NOT EXISTS borrowers (
        id TEXT PRIMARY KEY,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
""")

cursor.execute("""
    CREATE TABLE IF NOT EXISTS vendors (
        id TEXT PRIMARY KEY,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
""")

cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
""")

cursor.execute("""
    CREATE TABLE IF NOT EXISTS kyc_sessions (
        id TEXT PRIMARY KEY,
        loan_application_id TEXT NOT NULL,
        borrower_id TEXT NOT NULL,
        vendor TEXT NOT NULL,
        vendor_session_id TEXT,
        verification_url TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        expires_at TIMESTAMP,
        metadata TEXT,
        FOREIGN KEY (borrower_id) REFERENCES borrowers(id),
        FOREIGN KEY (loan_application_id) REFERENCES loan_applications(id)
    )
""")

cursor.execute("""
    CREATE TABLE IF NOT EXISTS kyc_results (
        id TEXT PRIMARY KEY,
        kyc_session_id TEXT NOT NULL,
        vendor TEXT NOT NULL,
        overall_status TEXT NOT NULL,
        check_type TEXT NOT NULL,
        check_status TEXT NOT NULL,
        check_details TEXT,
        score REAL,
        flags TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (kyc_session_id) REFERENCES kyc_sessions(id) ON DELETE CASCADE
    )
""")

cursor.execute("""
    CREATE TABLE IF NOT EXISTS kyc_events (
        id TEXT PRIMARY KEY,
        kyc_session_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        vendor_event_id TEXT,
        payload TEXT NOT NULL,
        processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (kyc_session_id) REFERENCES kyc_sessions(id) ON DELETE CASCADE
    )
""")

cursor.execute("""
    CREATE TABLE IF NOT EXISTS kyc_co_borrower_links (
        id TEXT PRIMARY KEY,
        loan_application_id TEXT NOT NULL,
        primary_kyc_session_id TEXT NOT NULL,
        co_borrower_kyc_session_id TEXT NOT NULL,
        co_borrower_role TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (loan_application_id) REFERENCES loan_applications(id),
        FOREIGN KEY (primary_kyc_session_id) REFERENCES kyc_sessions(id),
        FOREIGN KEY (co_borrower_kyc_session_id) REFERENCES kyc_sessions(id)
    )
""")

cursor.execute("""
    CREATE TABLE IF NOT EXISTS manual_kyb_reviews (
        id TEXT PRIMARY KEY,
        vendor_id TEXT NOT NULL,
        business_name TEXT NOT NULL,
        business_structure TEXT NOT NULL,
        business_registration_number TEXT,
        status TEXT NOT NULL DEFAULT 'pending_submission',
        submitted_by TEXT,
        reviewed_by TEXT,
        documents TEXT,
        beneficial_owners TEXT,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (vendor_id) REFERENCES vendors(id),
        FOREIGN KEY (submitted_by) REFERENCES users(id),
        FOREIGN KEY (reviewed_by) REFERENCES users(id)
    )
""")

conn.commit()
conn.close()

print(f"Database created at {DB_PATH}")