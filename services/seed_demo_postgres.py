"""
Create and populate a local Postgres database with fake SaaS sales data.

This is test-only scaffolding. The Django app does not import this module.
"""

from __future__ import annotations

import os
import random
from datetime import date, datetime, timedelta
from decimal import Decimal

import psycopg


DEFAULT_DATABASE_URL = (
    "postgresql://save_vibes_demo:save_vibes_demo@localhost:55432/save_vibes_demo"
)

DATABASE_URL = os.getenv("DEMO_DATABASE_URL", DEFAULT_DATABASE_URL)
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


def execute_schema(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(
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
                id SERIAL PRIMARY KEY,
                full_name TEXT NOT NULL,
                region TEXT NOT NULL,
                segment TEXT NOT NULL,
                start_date DATE NOT NULL
            );

            CREATE TABLE accounts (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                industry TEXT NOT NULL,
                region TEXT NOT NULL,
                segment TEXT NOT NULL,
                employee_count INTEGER NOT NULL,
                annual_revenue NUMERIC(14, 2) NOT NULL,
                created_at DATE NOT NULL
            );

            CREATE TABLE contacts (
                id SERIAL PRIMARY KEY,
                account_id INTEGER NOT NULL REFERENCES accounts(id),
                full_name TEXT NOT NULL,
                title TEXT NOT NULL,
                email TEXT NOT NULL,
                is_primary BOOLEAN NOT NULL DEFAULT FALSE
            );

            CREATE TABLE pipeline_stages (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                probability NUMERIC(5, 2) NOT NULL,
                sort_order INTEGER NOT NULL
            );

            CREATE TABLE opportunities (
                id SERIAL PRIMARY KEY,
                account_id INTEGER NOT NULL REFERENCES accounts(id),
                sales_rep_id INTEGER NOT NULL REFERENCES sales_reps(id),
                stage_id INTEGER NOT NULL REFERENCES pipeline_stages(id),
                name TEXT NOT NULL,
                lead_source TEXT NOT NULL,
                amount NUMERIC(14, 2) NOT NULL,
                expected_close_date DATE NOT NULL,
                created_at DATE NOT NULL,
                closed_at DATE,
                is_new_business BOOLEAN NOT NULL
            );

            CREATE TABLE activities (
                id SERIAL PRIMARY KEY,
                opportunity_id INTEGER NOT NULL REFERENCES opportunities(id),
                sales_rep_id INTEGER NOT NULL REFERENCES sales_reps(id),
                activity_type TEXT NOT NULL,
                occurred_at TIMESTAMP NOT NULL,
                notes TEXT NOT NULL
            );

            CREATE TABLE bookings (
                id SERIAL PRIMARY KEY,
                opportunity_id INTEGER NOT NULL UNIQUE REFERENCES opportunities(id),
                account_id INTEGER NOT NULL REFERENCES accounts(id),
                sales_rep_id INTEGER NOT NULL REFERENCES sales_reps(id),
                booked_at DATE NOT NULL,
                amount NUMERIC(14, 2) NOT NULL
            );

            CREATE TABLE quotas (
                id SERIAL PRIMARY KEY,
                sales_rep_id INTEGER NOT NULL REFERENCES sales_reps(id),
                quarter TEXT NOT NULL,
                quota_amount NUMERIC(14, 2) NOT NULL,
                UNIQUE (sales_rep_id, quarter)
            );
            """
        )


def insert_returning_id(cur: psycopg.Cursor, sql: str, params: tuple) -> int:
    cur.execute(sql + " RETURNING id", params)
    row = cur.fetchone()
    return int(row[0])


def quarter_for(day: date) -> str:
    quarter = ((day.month - 1) // 3) + 1
    return f"{day.year}-Q{quarter}"


def seed_data(conn: psycopg.Connection) -> dict[str, int]:
    random.seed(RANDOM_SEED)
    today = date.today()
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

    with conn.cursor() as cur:
        stage_ids = {}
        for name, probability, sort_order in STAGES:
            stage_id = insert_returning_id(
                cur,
                """
                INSERT INTO pipeline_stages (name, probability, sort_order)
                VALUES (%s, %s, %s)
                """,
                (name, Decimal(str(probability)), sort_order),
            )
            stage_ids[name] = stage_id
            counts["pipeline_stages"] += 1

        rep_ids = []
        for index in range(12):
            full_name = f"{FIRST_NAMES[index]} {LAST_NAMES[-index - 1]}"
            rep_id = insert_returning_id(
                cur,
                """
                INSERT INTO sales_reps (full_name, region, segment, start_date)
                VALUES (%s, %s, %s, %s)
                """,
                (
                    full_name,
                    REGIONS[index % len(REGIONS)],
                    random.choice(["SMB", "Mid-Market", "Enterprise"]),
                    today - timedelta(days=random.randint(180, 1800)),
                ),
            )
            rep_ids.append(rep_id)
            counts["sales_reps"] += 1

        account_ids = []
        for account_name in ACCOUNT_NAMES:
            account_id = insert_returning_id(
                cur,
                """
                INSERT INTO accounts
                    (name, industry, region, segment, employee_count, annual_revenue, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    account_name,
                    random.choice(INDUSTRIES),
                    random.choice(REGIONS),
                    random.choice(["SMB", "Mid-Market", "Enterprise"]),
                    random.randint(50, 9500),
                    Decimal(random.randrange(5_000_000, 950_000_000)),
                    today - timedelta(days=random.randint(90, 1500)),
                ),
            )
            account_ids.append(account_id)
            counts["accounts"] += 1

            contact_count = random.randint(2, 4)
            for contact_index in range(contact_count):
                first = random.choice(FIRST_NAMES)
                last = random.choice(LAST_NAMES)
                email_domain = account_name.lower().replace(" ", "").replace(",", "")
                cur.execute(
                    """
                    INSERT INTO contacts
                        (account_id, full_name, title, email, is_primary)
                    VALUES (%s, %s, %s, %s, %s)
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
                        contact_index == 0,
                    ),
                )
                counts["contacts"] += 1

        quarters = ["2025-Q3", "2025-Q4", "2026-Q1", "2026-Q2"]
        for rep_id in rep_ids:
            for quarter in quarters:
                cur.execute(
                    """
                    INSERT INTO quotas (sales_rep_id, quarter, quota_amount)
                    VALUES (%s, %s, %s)
                    """,
                    (
                        rep_id,
                        quarter,
                        Decimal(random.randrange(350_000, 1_300_000)),
                    ),
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
                closed_at
                if closed_at
                else today + timedelta(days=random.randint(7, 150))
            )
            account_id = random.choice(account_ids)
            rep_id = random.choice(rep_ids)
            amount = Decimal(random.randrange(12_000, 450_000))
            opportunity_id = insert_returning_id(
                cur,
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
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    account_id,
                    rep_id,
                    stage_ids[stage_name],
                    f"{random.choice(['Expansion', 'Platform', 'Analytics', 'Automation'])} Deal {index + 1}",
                    random.choice(LEAD_SOURCES),
                    amount,
                    expected_close,
                    created_at,
                    closed_at,
                    random.choice([True, True, False]),
                ),
            )
            counts["opportunities"] += 1

            for _activity_index in range(random.randint(1, 6)):
                occurred_at = datetime.combine(
                    created_at + timedelta(days=random.randint(0, 120)),
                    datetime.min.time(),
                ) + timedelta(hours=random.randint(8, 17), minutes=random.choice([0, 15, 30, 45]))
                cur.execute(
                    """
                    INSERT INTO activities
                        (opportunity_id, sales_rep_id, activity_type, occurred_at, notes)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        opportunity_id,
                        rep_id,
                        random.choice(ACTIVITY_TYPES),
                        occurred_at,
                        "Demo dataset activity",
                    ),
                )
                counts["activities"] += 1

            if stage_name == "Closed Won":
                cur.execute(
                    """
                    INSERT INTO bookings
                        (opportunity_id, account_id, sales_rep_id, booked_at, amount)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        opportunity_id,
                        account_id,
                        rep_id,
                        closed_at,
                        amount,
                    ),
                )
                counts["bookings"] += 1

    return counts


def main() -> None:
    print(f"Connecting to {DATABASE_URL}")
    with psycopg.connect(DATABASE_URL) as conn:
        execute_schema(conn)
        counts = seed_data(conn)
        conn.commit()

    print("Demo Postgres database populated:")
    for table_name, count in counts.items():
        print(f"  {table_name}: {count}")


if __name__ == "__main__":
    main()
