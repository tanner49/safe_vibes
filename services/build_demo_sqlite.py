"""
Build the bundled SQLite demo database used as the default demo data source.

This is maintenance tooling. The generated file is intentionally committed so
new local and Heroku environments can query demo sales data without running
Postgres-side seed scripts.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DEMO_DATABASE_PATH = BASE_DIR / "demo_data" / "demo_sales.sqlite3"
RANDOM_SEED = 20260608


ACCOUNT_NAMES = [
    "Acme Analytics",
    "Brightpath Logistics",
    "Cobalt Cloud",
    "Northstar Foods",
    "Lumen Health",
    "Evergreen Robotics",
    "Summit Retail",
    "Harbor Financial",
    "Canyon BioSystems",
    "Prairie Energy",
    "MetroStack",
    "Keystone Manufacturing",
    "Nimbus Travel",
    "Atlas Education",
    "Redwood Legal",
    "Orbit Media",
    "Silverline Security",
    "Pioneer Insurance",
    "Bluebird Payments",
    "VectorWorks",
    "Horizon Dental",
    "Meridian Apparel",
    "Apex Construction",
    "SignalPath",
    "Clearwater Utilities",
    "Bridgeway Staffing",
    "Granite Telecom",
    "Openfield AgTech",
    "Stratus Labs",
    "Forge Automotive",
]

FIRST_NAMES = [
    "Alex",
    "Avery",
    "Casey",
    "Drew",
    "Emery",
    "Finley",
    "Harper",
    "Jordan",
    "Kai",
    "Morgan",
    "Parker",
    "Quinn",
    "Riley",
    "Rowan",
    "Sage",
    "Taylor",
]

LAST_NAMES = [
    "Adams",
    "Bennett",
    "Chen",
    "Diaz",
    "Ellis",
    "Foster",
    "Garcia",
    "Hayes",
    "Ivanov",
    "Jones",
    "Khan",
    "Lee",
    "Miller",
    "Nguyen",
    "Patel",
    "Rivera",
]

INDUSTRIES = [
    "Technology",
    "Healthcare",
    "Financial Services",
    "Manufacturing",
    "Retail",
    "Education",
    "Energy",
    "Media",
]

REGIONS = ["West", "Mountain", "Midwest", "South", "Northeast", "International"]

STAGES = [
    ("Prospecting", 0.10, 1),
    ("Qualified", 0.25, 2),
    ("Solution Fit", 0.40, 3),
    ("Proposal", 0.60, 4),
    ("Negotiation", 0.80, 5),
    ("Closed Won", 1.00, 6),
    ("Closed Lost", 0.00, 7),
]

ACTIVITY_TYPES = ["Call", "Email", "Demo", "Executive meeting", "Proposal review"]
LEAD_SOURCES = ["Inbound", "Outbound", "Partner", "Event", "Referral", "Expansion"]


def execute_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS bookings;
        DROP TABLE IF EXISTS activities;
        DROP TABLE IF EXISTS opportunities;
        DROP TABLE IF EXISTS contacts;
        DROP TABLE IF EXISTS quotas;
        DROP TABLE IF EXISTS accounts;
        DROP TABLE IF EXISTS sales_reps;
        DROP TABLE IF EXISTS pipeline_stages;

        CREATE TABLE sales_reps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            region TEXT NOT NULL,
            segment TEXT NOT NULL,
            start_date TEXT NOT NULL
        );

        CREATE TABLE accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            industry TEXT NOT NULL,
            region TEXT NOT NULL,
            segment TEXT NOT NULL,
            employee_count INTEGER NOT NULL,
            annual_revenue REAL NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL REFERENCES accounts(id),
            full_name TEXT NOT NULL,
            title TEXT NOT NULL,
            email TEXT NOT NULL,
            is_primary INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE pipeline_stages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            probability REAL NOT NULL,
            sort_order INTEGER NOT NULL
        );

        CREATE TABLE opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL REFERENCES accounts(id),
            sales_rep_id INTEGER NOT NULL REFERENCES sales_reps(id),
            stage_id INTEGER NOT NULL REFERENCES pipeline_stages(id),
            name TEXT NOT NULL,
            lead_source TEXT NOT NULL,
            amount REAL NOT NULL,
            expected_close_date TEXT NOT NULL,
            created_at TEXT NOT NULL,
            closed_at TEXT,
            is_new_business INTEGER NOT NULL
        );

        CREATE TABLE activities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opportunity_id INTEGER NOT NULL REFERENCES opportunities(id),
            sales_rep_id INTEGER NOT NULL REFERENCES sales_reps(id),
            activity_type TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            notes TEXT NOT NULL
        );

        CREATE TABLE bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opportunity_id INTEGER NOT NULL UNIQUE REFERENCES opportunities(id),
            account_id INTEGER NOT NULL REFERENCES accounts(id),
            sales_rep_id INTEGER NOT NULL REFERENCES sales_reps(id),
            booked_at TEXT NOT NULL,
            amount REAL NOT NULL
        );

        CREATE TABLE quotas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sales_rep_id INTEGER NOT NULL REFERENCES sales_reps(id),
            quarter TEXT NOT NULL,
            quota_amount REAL NOT NULL,
            UNIQUE (sales_rep_id, quarter)
        );
        """
    )


def insert_row(conn: sqlite3.Connection, sql: str, params: tuple) -> int:
    cursor = conn.execute(sql, params)
    return int(cursor.lastrowid)


def seed_data(conn: sqlite3.Connection) -> dict[str, int]:
    random.seed(RANDOM_SEED)
    today = date(2026, 6, 9)
    counts = {
        "sales_reps": 0,
        "accounts": 0,
        "contacts": 0,
        "pipeline_stages": 0,
        "opportunities": 0,
        "activities": 0,
        "bookings": 0,
        "quotas": 0,
    }

    stage_ids = {}
    for name, probability, sort_order in STAGES:
        stage_ids[name] = insert_row(
            conn,
            "INSERT INTO pipeline_stages (name, probability, sort_order) VALUES (?, ?, ?)",
            (name, probability, sort_order),
        )
        counts["pipeline_stages"] += 1

    rep_ids = []
    for index in range(12):
        rep_ids.append(
            insert_row(
                conn,
                "INSERT INTO sales_reps (full_name, region, segment, start_date) VALUES (?, ?, ?, ?)",
                (
                    f"{FIRST_NAMES[index]} {LAST_NAMES[-index - 1]}",
                    REGIONS[index % len(REGIONS)],
                    random.choice(["SMB", "Mid-Market", "Enterprise"]),
                    (today - timedelta(days=random.randint(180, 1800))).isoformat(),
                ),
            )
        )
        counts["sales_reps"] += 1

    account_ids = []
    for account_name in ACCOUNT_NAMES:
        account_id = insert_row(
            conn,
            """
            INSERT INTO accounts
                (name, industry, region, segment, employee_count, annual_revenue, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_name,
                random.choice(INDUSTRIES),
                random.choice(REGIONS),
                random.choice(["SMB", "Mid-Market", "Enterprise"]),
                random.randint(50, 9500),
                random.randrange(5_000_000, 950_000_000),
                (today - timedelta(days=random.randint(90, 1500))).isoformat(),
            ),
        )
        account_ids.append(account_id)
        counts["accounts"] += 1

        for contact_index in range(random.randint(2, 4)):
            first = random.choice(FIRST_NAMES)
            last = random.choice(LAST_NAMES)
            email_domain = account_name.lower().replace(" ", "").replace(",", "")
            conn.execute(
                """
                INSERT INTO contacts
                    (account_id, full_name, title, email, is_primary)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    f"{first} {last}",
                    random.choice(
                        [
                            "VP Sales",
                            "CFO",
                            "Revenue Operations Lead",
                            "Director of Analytics",
                            "COO",
                        ]
                    ),
                    f"{first.lower()}.{last.lower()}@{email_domain}.example",
                    1 if contact_index == 0 else 0,
                ),
            )
            counts["contacts"] += 1

    for rep_id in rep_ids:
        for quarter in ["2025-Q3", "2025-Q4", "2026-Q1", "2026-Q2"]:
            conn.execute(
                "INSERT INTO quotas (sales_rep_id, quarter, quota_amount) VALUES (?, ?, ?)",
                (rep_id, quarter, random.randrange(350_000, 1_300_000)),
            )
            counts["quotas"] += 1

    open_stage_names = [stage[0] for stage in STAGES[:5]]
    closed_stage_names = ["Closed Won", "Closed Lost"]
    for index in range(180):
        created_at = today - timedelta(days=random.randint(15, 420))
        stage_name = random.choices(
            open_stage_names + closed_stage_names,
            weights=[18, 18, 16, 12, 10, 14, 12],
            k=1,
        )[0]
        is_closed = stage_name in closed_stage_names
        closed_at = (
            created_at + timedelta(days=random.randint(20, 180))
            if is_closed
            else None
        )
        expected_close = (
            closed_at if closed_at else today + timedelta(days=random.randint(7, 150))
        )
        account_id = random.choice(account_ids)
        rep_id = random.choice(rep_ids)
        amount = random.randrange(12_000, 450_000)
        opportunity_id = insert_row(
            conn,
            """
            INSERT INTO opportunities
                (
                    account_id,
                    sales_rep_id,
                    stage_id,
                    name,
                    lead_source,
                    amount,
                    expected_close_date,
                    created_at,
                    closed_at,
                    is_new_business
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id,
                rep_id,
                stage_ids[stage_name],
                f"{random.choice(['Expansion', 'Platform', 'Analytics', 'Automation'])} Deal {index + 1}",
                random.choice(LEAD_SOURCES),
                amount,
                expected_close.isoformat(),
                created_at.isoformat(),
                closed_at.isoformat() if closed_at else None,
                1 if random.choice([True, True, False]) else 0,
            ),
        )
        counts["opportunities"] += 1

        for _activity_index in range(random.randint(1, 6)):
            occurred_at = datetime.combine(
                created_at + timedelta(days=random.randint(0, 120)),
                datetime.min.time(),
            ) + timedelta(hours=random.randint(8, 17), minutes=random.choice([0, 15, 30, 45]))
            conn.execute(
                """
                INSERT INTO activities
                    (opportunity_id, sales_rep_id, activity_type, occurred_at, notes)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    opportunity_id,
                    rep_id,
                    random.choice(ACTIVITY_TYPES),
                    occurred_at.isoformat(sep=" "),
                    "Demo dataset activity",
                ),
            )
            counts["activities"] += 1

        if stage_name == "Closed Won":
            conn.execute(
                """
                INSERT INTO bookings
                    (opportunity_id, account_id, sales_rep_id, booked_at, amount)
                VALUES (?, ?, ?, ?, ?)
                """,
                (opportunity_id, account_id, rep_id, closed_at.isoformat(), amount),
            )
            counts["bookings"] += 1

    return counts


def main() -> None:
    DEMO_DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DEMO_DATABASE_PATH.exists():
        DEMO_DATABASE_PATH.unlink()
    with sqlite3.connect(DEMO_DATABASE_PATH) as conn:
        execute_schema(conn)
        counts = seed_data(conn)
        conn.commit()

    print(f"Demo SQLite database written to {DEMO_DATABASE_PATH}")
    for table_name, count in counts.items():
        print(f"  {table_name}: {count}")


if __name__ == "__main__":
    main()
