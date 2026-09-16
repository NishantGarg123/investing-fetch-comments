"""
CLI Script to test and fetch AWS Billing for ECS Fargate in us-west-2
Usage:
    python test_aws_billing.py
"""
import os
import sys
import json
from datetime import datetime, timedelta
import boto3
from dotenv import load_dotenv

load_dotenv()

def fetch_fargate_billing(region="us-west-2", start_date=None, end_date=None):
    aws_access_key = os.getenv("AWS_ACCESS_KEY_ID")
    aws_secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
    aws_session_token = os.getenv("AWS_SESSION_TOKEN")

    if not aws_access_key or not aws_secret_key:
        print("❌ Error: AWS_ACCESS_KEY_ID or AWS_SECRET_ACCESS_KEY not set in .env file.")
        sys.exit(1)

    today = datetime.utcnow().date()
    if not start_date:
        start_date = today.replace(day=1).strftime("%Y-%m-%d")
    if not end_date:
        end_date = (today + timedelta(days=1)).strftime("%Y-%m-%d")

    print(f"📊 Querying AWS Cost Explorer...")
    print(f"   Service: AWS ECS Fargate")
    print(f"   Region:  {region}")
    print(f"   Period:  {start_date} to {end_date} (exclusive)\n")

    client = boto3.client(
        "ce",
        region_name="us-east-1",
        aws_access_key_id=aws_access_key.strip(),
        aws_secret_access_key=aws_secret_key.strip(),
        aws_session_token=aws_session_token.strip() if aws_session_token else None
    )

    cost_filter = {
        "And": [
            {"Dimensions": {"Key": "REGION", "Values": [region]}},
            {
                "Or": [
                    {"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Elastic Container Service", "Amazon Elastic Container Service - Fargate"]}},
                    {"Dimensions": {"Key": "USAGE_TYPE_GROUP", "Values": ["EC2: Fargate - Running Hours"]}}
                ]
            }
        ]
    }

    try:
        response = client.get_cost_and_usage(
            TimePeriod={"Start": start_date, "End": end_date},
            Granularity="DAILY",
            Filter=cost_filter,
            Metrics=["UnblendedCost", "UsageQuantity"],
            GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}]
        )

        total_cost = 0.0
        for period in response.get("ResultsByTime", []):
            time_start = period.get("TimePeriod", {}).get("Start")
            time_end = period.get("TimePeriod", {}).get("End")
            period_cost = 0.0
            print(f"📅 [{time_start} - {time_end}]")
            for group in period.get("Groups", []):
                usage = group["Keys"][0]
                cost = float(group.get("Metrics", {}).get("UnblendedCost", {}).get("Amount", 0.0))
                qty = float(group.get("Metrics", {}).get("UsageQuantity", {}).get("Amount", 0.0))
                period_cost += cost
                if cost > 0 or qty > 0:
                    print(f"   • {usage}: ${cost:.6f} (Usage: {qty:.4f})")
            print(f"   -> Daily Total: ${period_cost:.4f}\n")
            total_cost += period_cost

        # Fetch and display summation for previous 3 months
        # Calculate 3 months back start date
        past_year = today.year
        past_month = today.month - 3
        while past_month <= 0:
            past_month += 12
            past_year -= 1
        past_start = f"{past_year:04d}-{past_month:02d}-01"

        print("=======================================")
        print(f"💰 Current Month-to-Date Total ({region}): ${total_cost:.4f} USD")
        print("=======================================\n")

        print("📊 Querying Previous 3 Months Totals...")
        history_response = client.get_cost_and_usage(
            TimePeriod={"Start": past_start, "End": end_date},
            Granularity="MONTHLY",
            Filter=cost_filter,
            Metrics=["UnblendedCost"]
        )

        curr_month_start = today.replace(day=1).strftime("%Y-%m-%d")
        print("📅 =======================================")
        print(f"📊 Previous 3 Months Cost Summary ({region}):")
        for period in history_response.get("ResultsByTime", []):
            p_start = period.get("TimePeriod", {}).get("Start")
            p_end = period.get("TimePeriod", {}).get("End")
            cost = float(period.get("Total", {}).get("UnblendedCost", {}).get("Amount", 0.0))
            try:
                start_dt = datetime.strptime(p_start, "%Y-%m-%d")
                label = start_dt.strftime("%m/%Y")
                name = start_dt.strftime("%B %Y")
            except Exception:
                label = p_start
                name = p_start

            if p_start != curr_month_start:
                print(f"   • {label} ({name}): ${cost:.4f} USD")
        print("📅 =======================================")

    except Exception as e:
        print(f"❌ Failed to fetch AWS Billing: {e}")

if __name__ == "__main__":
    fetch_fargate_billing()
