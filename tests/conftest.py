import os
from collections.abc import Iterator
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

from tests.fakes import FakeTelegram
from tvtg import config, ingest, sender

REGION = "us-west-2"
CHAT_ID = "-1001111111111"
CANARY_CHAT_ID = "-1002222222222"


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No real AWS credentials or leftover module state can leak between tests."""
    for key in list(os.environ):
        if key.startswith("AWS_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    for key in ("ALLOWED_IPS", "ALLOWED_IPS_PARAM", "ALLOW_ANY_IP", "CANARY_CHAT_ID", "TIMING_TABLE"):
        monkeypatch.delenv(key, raising=False)
    config.reset()
    sender.reset()
    ingest._warm = False
    yield
    config.reset()
    sender.reset()


@pytest.fixture
def telegram() -> Iterator[FakeTelegram]:
    fake = FakeTelegram().start()
    yield fake
    fake.stop()


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch, telegram: FakeTelegram) -> Iterator[SimpleNamespace]:
    """moto-backed SSM, SQS and DynamoDB, wired up exactly as Terraform configures the Lambdas."""
    with mock_aws():
        ssm = boto3.client("ssm", region_name=REGION)
        for name, value in (("/tvtg/test/tv_secret", "tv-secret"), ("/tvtg/test/bot_token", "123:ABC"),
                            ("/tvtg/test/collector_key", "collector-key")):
            ssm.put_parameter(Name=name, Value=value, Type="SecureString")

        sqs = boto3.client("sqs", region_name=REGION)
        queue_url = sqs.create_queue(QueueName="tvtg-test-alerts")["QueueUrl"]

        ddb = boto3.client("dynamodb", region_name=REGION)
        ddb.create_table(TableName="tvtg-test-timing", BillingMode="PAY_PER_REQUEST",
                         AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
                         KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}])

        env = {
            "QUEUE_URL": queue_url,
            "TV_SECRET_PARAM": "/tvtg/test/tv_secret",
            "BOT_TOKEN_PARAM": "/tvtg/test/bot_token",
            "COLLECTOR_KEY_PARAM": "/tvtg/test/collector_key",
            "CHAT_ID": CHAT_ID,
            "TIMING_TABLE": "tvtg-test-timing",
            "TELEGRAM_BASE_URL": telegram.url,
        }
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        yield SimpleNamespace(ssm=ssm, sqs=sqs, ddb=ddb, table="tvtg-test-timing",
                              telegram=telegram, **{k.lower(): v for k, v in env.items()})
