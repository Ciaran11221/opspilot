"""
Synthetic data generator for OpsPilot.

Generates two datasets that mimic the *shape* of real exports (Okta/M365-style
user directory, Jira-style ticket export) without containing any real
organization's data. A fixed random seed guarantees the same "ground truth"
scenarios every time, which is what the demo trace panel narrates against.

Run:
    python generate_data.py

Outputs:
    accounts.json
    tickets.json
"""
import json
import random
from datetime import datetime, timedelta, timezone

random.seed(1337)

FIRST_NAMES = [
    "Aoife", "Cian", "Niamh", "Sean", "Orla", "Liam", "Saoirse", "Eoin",
    "Roisin", "Cormac", "Grainne", "Fionn", "Aisling", "Declan", "Maeve",
    "Ronan", "Sinead", "Padraig", "Ciara", "Tadhg",
]
LAST_NAMES = [
    "Byrne", "Kelly", "Doyle", "Walsh", "OSullivan", "Ryan", "Murphy",
    "Connolly", "Gallagher", "Fitzgerald", "Nolan", "Brennan", "Dunne",
    "Healy", "Hogan",
]
DEPARTMENTS = ["Engineering", "Sales", "Support", "Finance", "People", "IT", "Marketing"]
ROLE_LEVELS = {
    "standard": ["Employee", "Support Analyst", "Account Executive", "Analyst"],
    "elevated": ["Admin", "Sys Admin", "Finance Manager", "IT Manager", "Super Admin"],
}
GROUPS_STANDARD = ["All-Staff", "VPN-Users", "Slack-All"]
GROUPS_ELEVATED = ["Domain-Admins", "Billing-Admins", "Okta-Super-Admins", "AWS-Root-Access", "M365-Global-Admins"]

NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)


def rand_date(days_ago_min, days_ago_max):
    days = random.randint(days_ago_min, days_ago_max)
    return (NOW - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_account(idx, force_scenario=None):
    first = random.choice(FIRST_NAMES)
    last = random.choice(LAST_NAMES)
    username = f"{first.lower()}.{last.lower()}{idx}"
    email = f"{username}@northwind-demo.com"

    scenario = force_scenario or random.choices(
        ["normal", "inactive_elevated", "inactive_standard", "active_elevated"],
        weights=[55, 12, 20, 13],
    )[0]

    if scenario == "inactive_elevated":
        last_login = rand_date(95, 240)
        status = "ACTIVE"  # account never disabled despite inactivity - the risk
        role = random.choice(ROLE_LEVELS["elevated"])
        groups = random.sample(GROUPS_ELEVATED, k=random.randint(1, 2)) + ["All-Staff"]
    elif scenario == "inactive_standard":
        last_login = rand_date(95, 200)
        status = "ACTIVE"
        role = random.choice(ROLE_LEVELS["standard"])
        groups = ["All-Staff"]
    elif scenario == "active_elevated":
        last_login = rand_date(0, 14)
        status = "ACTIVE"
        role = random.choice(ROLE_LEVELS["elevated"])
        groups = random.sample(GROUPS_ELEVATED, k=random.randint(1, 2)) + ["All-Staff"]
    else:  # normal
        last_login = rand_date(0, 30)
        status = "ACTIVE"
        role = random.choice(ROLE_LEVELS["standard"])
        groups = ["All-Staff"] + (["VPN-Users"] if random.random() > 0.5 else [])

    return {
        "id": f"00u{idx:06d}",
        "username": username,
        "email": email,
        "displayName": f"{first} {last}",
        "department": random.choice(DEPARTMENTS),
        "title": role,
        "status": status,
        "lastLogin": last_login,
        "created": rand_date(200, 1500),
        "groups": groups,
        "mfaEnrolled": random.random() > 0.15,
        "_scenarioTag": scenario,  # ground-truth label, stripped before "export" in README note
    }


def make_ticket(idx, force_scenario=None):
    scenario = force_scenario or random.choices(
        ["sla_risk_high", "sla_risk_medium", "healthy", "resolved"],
        weights=[10, 15, 45, 30],
    )[0]

    created_days_ago = random.randint(0, 20)
    priority = random.choice(["P1", "P2", "P3", "P4"])
    sla_hours_map = {"P1": 4, "P2": 24, "P3": 72, "P4": 168}
    sla_hours = sla_hours_map[priority]

    if scenario == "sla_risk_high":
        # elapsed time is >90% of SLA window, still open
        elapsed_hours = sla_hours * random.uniform(0.9, 1.15)
        status = "In Progress" if elapsed_hours < sla_hours else "In Progress"
    elif scenario == "sla_risk_medium":
        elapsed_hours = sla_hours * random.uniform(0.6, 0.89)
        status = "In Progress"
    elif scenario == "resolved":
        elapsed_hours = sla_hours * random.uniform(0.2, 0.8)
        status = "Resolved"
    else:  # healthy
        elapsed_hours = sla_hours * random.uniform(0.05, 0.55)
        status = random.choice(["Open", "In Progress"])

    created = NOW - timedelta(hours=created_days_ago * 24)
    updated = created + timedelta(hours=min(elapsed_hours, created_days_ago * 24))

    summaries = [
        "Cannot access shared drive after password reset",
        "New hire laptop provisioning - Engineering",
        "VPN client failing to connect on VPN-Users group",
        "Okta MFA re-enrollment request",
        "Offboarding: revoke access for departing contractor",
        "Shared mailbox permissions incorrect",
        "Printer driver rollout - Galway office",
        "Software license request - Adobe CC",
        "Account lockout after failed login attempts",
        "Request to join Billing-Admins group",
    ]

    return {
        "key": f"OPS-{1000 + idx}",
        "summary": random.choice(summaries),
        "priority": priority,
        "status": status,
        "assignee": f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}",
        "reporterEmail": f"user{idx}@northwind-demo.com",
        "created": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "updated": updated.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "slaHours": sla_hours,
        "elapsedHours": round(elapsed_hours, 1),
        "_scenarioTag": scenario,
    }


def main():
    accounts = []
    # Seed guaranteed scenarios first so the demo always has clear hits
    for i, scen in enumerate(["inactive_elevated"] * 4 + ["inactive_standard"] * 3 + ["active_elevated"] * 2):
        accounts.append(make_account(i, force_scenario=scen))
    for i in range(len(accounts), 60):
        accounts.append(make_account(i))
    random.shuffle(accounts)

    tickets = []
    for i, scen in enumerate(["sla_risk_high"] * 3 + ["sla_risk_medium"] * 4):
        tickets.append(make_ticket(i, force_scenario=scen))
    for i in range(len(tickets), 40):
        tickets.append(make_ticket(i))
    random.shuffle(tickets)

    with open("accounts.json", "w") as f:
        json.dump(accounts, f, indent=2)
    with open("tickets.json", "w") as f:
        json.dump(tickets, f, indent=2)

    ie = sum(1 for a in accounts if a["_scenarioTag"] == "inactive_elevated")
    sr = sum(1 for t in tickets if t["_scenarioTag"] in ("sla_risk_high", "sla_risk_medium"))
    print(f"Generated {len(accounts)} accounts ({ie} inactive+elevated ground truth)")
    print(f"Generated {len(tickets)} tickets ({sr} at SLA risk ground truth)")


if __name__ == "__main__":
    main()
