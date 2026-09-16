import os
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from fastapi import APIRouter, HTTPException, Header, Query, Depends, status
from fastapi.security import APIKeyHeader, HTTPBearer, HTTPAuthorizationCredentials
import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from dotenv import load_dotenv

load_dotenv()

router = APIRouter(prefix="/api/billing", tags=["AWS Billing"])

# API Key Security Schemes
api_key_header_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)
http_bearer_scheme = HTTPBearer(auto_error=False)


def verify_api_key(
    x_api_key: Optional[str] = Depends(api_key_header_scheme),
    bearer_auth: Optional[HTTPAuthorizationCredentials] = Depends(http_bearer_scheme),
    query_key: Optional[str] = Query(None, alias="api_key", description="API key passed as query parameter")
) -> str:
    """
    Validates API key provided in:
    1. 'X-API-Key' Header
    2. 'Authorization: Bearer <API_KEY>' Header
    3. '?api_key=<API_KEY>' Query Parameter
    """
    expected_api_key = os.getenv("API_KEY") or os.getenv("BILLING_API_KEY")

    # If no API key configured in .env, allow for development or notify
    if not expected_api_key:
        return "development_unrestricted"

    # Extract provided key
    provided_key = None
    if x_api_key:
        provided_key = x_api_key.strip()
    elif bearer_auth and bearer_auth.credentials:
        provided_key = bearer_auth.credentials.strip()
    elif query_key:
        provided_key = query_key.strip()

    if not provided_key or provided_key != expected_api_key.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key. Provide via 'X-API-Key' header, Bearer token, or 'api_key' query parameter."
        )

    return provided_key


def get_cost_explorer_client() -> boto3.client:
    """
    Initializes AWS Cost Explorer client using credentials from environment variables.
    AWS Cost Explorer API endpoint is global and hosted in us-east-1.
    """
    aws_access_key = os.getenv("AWS_ACCESS_KEY_ID")
    aws_secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
    aws_session_token = os.getenv("AWS_SESSION_TOKEN")

    if not aws_access_key or not aws_secret_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AWS credentials missing. Please set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY in your .env file."
        )

    try:
        return boto3.client(
            "ce",
            region_name="us-east-1",  # Cost Explorer endpoint is always us-east-1
            aws_access_key_id=aws_access_key.strip(),
            aws_secret_access_key=aws_secret_key.strip(),
            aws_session_token=aws_session_token.strip() if aws_session_token else None
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to initialize AWS Cost Explorer client: {str(e)}"
        )


@router.get("/ecs-fargate", summary="Get AWS ECS Fargate Billing in us-west-2")
def get_ecs_fargate_billing(
    start_date: Optional[str] = Query(
        None,
        description="Start date in YYYY-MM-DD format (inclusive). Defaults to 1st day of current month."
    ),
    end_date: Optional[str] = Query(
        None,
        description="End date in YYYY-MM-DD format (exclusive). Defaults to tomorrow."
    ),
    granularity: str = Query(
        "DAILY",
        regex="^(DAILY|MONTHLY)$",
        description="Cost aggregation interval: DAILY or MONTHLY"
    ),
    region: str = Query("us-west-2", description="AWS Region (default: us-west-2)"),
    _auth: str = Depends(verify_api_key)
) -> Dict[str, Any]:
    """
    Retrieves detailed billing and usage breakdown for AWS ECS Fargate in us-west-2
    from AWS Cost Explorer.
    """
    today = datetime.utcnow().date()
    if not start_date:
        start_date = today.replace(day=1).strftime("%Y-%m-%d")
    if not end_date:
        # AWS Cost Explorer end date is exclusive
        end_date = (today + timedelta(days=1)).strftime("%Y-%m-%d")

    client = get_cost_explorer_client()

    # AWS Cost Explorer filter for ECS Fargate in target region
    cost_filter = {
        "And": [
            {
                "Dimensions": {
                    "Key": "REGION",
                    "Values": [region]
                }
            },
            {
                "Or": [
                    {
                        "Dimensions": {
                            "Key": "SERVICE",
                            "Values": [
                                "Amazon Elastic Container Service",
                                "Amazon Elastic Container Service - Fargate"
                            ]
                        }
                    },
                    {
                        "Dimensions": {
                            "Key": "USAGE_TYPE_GROUP",
                            "Values": ["EC2: Fargate - Running Hours"]
                        }
                    }
                ]
            }
        ]
    }

    try:
        response = client.get_cost_and_usage(
            TimePeriod={"Start": start_date, "End": end_date},
            Granularity=granularity,
            Filter=cost_filter,
            Metrics=["UnblendedCost", "UsageQuantity"],
            GroupBy=[
                {"Type": "DIMENSION", "Key": "USAGE_TYPE"}
            ]
        )

        results_by_time = []
        total_cost_usd = 0.0

        for period in response.get("ResultsByTime", []):
            period_start = period.get("TimePeriod", {}).get("Start")
            period_end = period.get("TimePeriod", {}).get("End")

            period_cost = 0.0
            breakdown = []

            for group in period.get("Groups", []):
                usage_type = group["Keys"][0]
                metrics = group.get("Metrics", {})
                cost = float(metrics.get("UnblendedCost", {}).get("Amount", 0.0))
                unit = metrics.get("UnblendedCost", {}).get("Unit", "USD")
                usage_qty = float(metrics.get("UsageQuantity", {}).get("Amount", 0.0))

                period_cost += cost
                breakdown.append({
                    "usage_type": usage_type,
                    "cost_usd": round(cost, 6),
                    "usage_quantity": round(usage_qty, 4),
                    "unit": unit
                })

            total_cost_usd += period_cost

            results_by_time.append({
                "time_period": {
                    "start": period_start,
                    "end": period_end
                },
                "total_cost_usd": round(period_cost, 4),
                "breakdown": breakdown
            })

        return {
            "status": "success",
            "service": "AWS ECS Fargate",
            "region": region,
            "granularity": granularity,
            "query_period": {
                "start_date": start_date,
                "end_date": end_date
            },
            "total_cost_usd": round(total_cost_usd, 4),
            "currency": "USD",
            "results": results_by_time
        }

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        error_msg = e.response.get("Error", {}).get("Message", str(e))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"AWS Cost Explorer Error [{error_code}]: {error_msg}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to query AWS billing: {str(e)}"
        )


@router.get("/summary", summary="Get Total Month-to-Date AWS ECS Fargate Cost")
def get_billing_summary(
    region: str = Query("us-west-2", description="AWS Region"),
    _auth: str = Depends(verify_api_key)
) -> Dict[str, Any]:
    """
    Convenience endpoint to get Month-To-Date (MTD) total cost for AWS ECS Fargate.
    """
    today = datetime.utcnow().date()
    start_date = today.replace(day=1).strftime("%Y-%m-%d")
    end_date = (today + timedelta(days=1)).strftime("%Y-%m-%d")

    client = get_cost_explorer_client()

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
            Granularity="MONTHLY",
            Filter=cost_filter,
            Metrics=["UnblendedCost"]
        )

        total_mtd_cost = 0.0
        for period in response.get("ResultsByTime", []):
            total_mtd_cost += float(period.get("Total", {}).get("UnblendedCost", {}).get("Amount", 0.0))

        return {
            "status": "success",
            "service": "AWS ECS Fargate",
            "region": region,
            "period": f"{start_date} to {today.strftime('%Y-%m-%d')}",
            "month_to_date_cost_usd": round(total_mtd_cost, 4),
            "currency": "USD"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
