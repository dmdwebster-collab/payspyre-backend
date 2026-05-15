#!/usr/bin/env python
"""Simple script to create test data for portal testing"""
import sqlite3
import uuid
from datetime import datetime, timedelta

def create_test_data():
    conn = sqlite3.connect('payspyre.db')
    cursor = conn.cursor()

    # Create a test user
    user_id = str(uuid.uuid4())
    cursor.execute('''
        INSERT INTO users (id, email, full_name, role, hashed_password, is_active, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, 'admin@payspyre.test', 'Test Admin', 'admin',
          'hashed_password_here', True, datetime.now(), datetime.now()))

    # Create a test vendor
    vendor_id = str(uuid.uuid4())
    cursor.execute('''
        INSERT INTO vendors (id, name, email, phone, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (vendor_id, 'Kelowna Dental Centre', 'contact@kdc.ca', '250-555-1234',
          'active', datetime.now(), datetime.now()))

    # Create a test borrower
    borrower_id = str(uuid.uuid4())
    cursor.execute('''
        INSERT INTO borrowers (id, email, first_name, last_name, phone, date_of_birth, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (borrower_id, 'john.doe@example.com', 'John', 'Doe', '250-555-5678',
          '1990-01-15', datetime.now(), datetime.now()))

    # Create a test loan application
    app_id = str(uuid.uuid4())
    cursor.execute('''
        INSERT INTO loan_applications (
            id, borrower_id, vendor_id, requested_amount, purpose, status,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (app_id, borrower_id, vendor_id, 5000.00, 'dental_implants',
          'pending_review', datetime.now(), datetime.now()))

    conn.commit()
    conn.close()

    print(f"Created test data:")
    print(f"  User: admin@payspyre.test (admin)")
    print(f"  Vendor: Kelowna Dental Centre")
    print(f"  Borrower: john.doe@example.com")
    print(f"  Application ID: {app_id}")
    print(f"  Status: pending_review")

if __name__ == '__main__':
    create_test_data()
