import asyncio
import json
import logging
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import unquote

import boto3

from profiler import Profiler
from splitter import Splitter


def parse_job(job):
    user_id = job.get("user_id") or job.get("userId")
    pdf_id = job.get("pdf_id") or job.get("pdfId")
    bucket = job.get("bucket") or job.get("Bucket")
    key = job.get("key") or job.get("pdfKey") or job.get("pdf_key")
    if not all(isinstance(value, str) and value for value in (user_id, pdf_id, bucket, key)):
        raise ValueError("Job requires user_id, pdf_id, bucket, and key")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", pdf_id):
        raise ValueError("Invalid PDF ID")
    match = re.fullmatch(r"([^/]+)/original/([^/]+)\.pdf", key, flags=re.IGNORECASE)
    if not match:
        key = unquote(key.replace("+", " "))
        match = re.fullmatch(r"([^/]+)/original/([^/]+)\.pdf", key, flags=re.IGNORECASE)
    if not match or match[1] != pdf_id:
        raise ValueError("Original PDF key must match the job PDF ID")
    return user_id, pdf_id, bucket, key, match[2]


async def process_job(job, s3, dynamodb, sqs, table_name, output_queue):
    user_id, pdf_id, bucket, original_key, pdf_name = parse_job(job)
    with tempfile.TemporaryDirectory(prefix="book-job-") as directory:
        pdf_path = Path(directory) / "original.pdf"
        s3.download_file(bucket, original_key, str(pdf_path))
        keys = Splitter(s3, dynamodb, table_name).pdf_split(
            pdf_path, bucket, user_id, pdf_id, pdf_name)
        profiler = Profiler(s3)
        state = await profiler.profile(
            pdf_path, bucket, pdf_id, len(keys), Path(directory) / "profile")
        enqueued = set(state.get("enqueuedPages", []))
        for page, key in enumerate(keys, start=1):
            if page in enqueued:
                continue
            sqs.send_message(QueueUrl=output_queue, MessageBody=json.dumps({
                "user_id": user_id, "pdf_id": pdf_id, "page": page,
                "bucket": bucket, "pdfS3Uri": f"s3://{bucket}/{key}",
            }))
            enqueued.add(page)
            state["enqueuedPages"] = sorted(enqueued)
            profiler._save(bucket, f"{pdf_id}/designs/catalog.json", state)
        logging.info("Book %s complete; %s conversion jobs recorded", pdf_id, len(enqueued))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    table_name = os.environ.get("DDB_TABLE_NAME")
    input_queue = os.environ.get("SQS_PDF_SPLIT_QUEUE_URL") or os.environ.get("SQS_QUEUE_URL")
    output_queue = os.environ.get("SQS_PAGE_CONVERSION_QUEUE_URL")
    
    if not all((table_name, input_queue, output_queue)):
        raise RuntimeError("DDB_TABLE_NAME, SQS_PDF_SPLIT_QUEUE_URL, and SQS_PAGE_CONVERSION_QUEUE_URL are required")
    if input_queue == output_queue:
        raise ValueError("Input and conversion queues must be different")
    
    s3 = boto3.client("s3")
    dynamodb = boto3.client("dynamodb")
    sqs = boto3.client("sqs")
    logging.info("Split/profile ECS worker polling %s", input_queue)
    while True:
        response = sqs.receive_message(
            QueueUrl=input_queue, MaxNumberOfMessages=1, WaitTimeSeconds=20,
        )
        for message in response.get("Messages", []):
            try:
                asyncio.run(process_job(json.loads(message["Body"]), s3, dynamodb, sqs,
                                        table_name, output_queue))
                sqs.delete_message(QueueUrl=input_queue, ReceiptHandle=message["ReceiptHandle"])
            except Exception:
                logging.exception("Book job failed; message retained for retry/DLQ: %s",
                                  message.get("MessageId"))


if __name__ == "__main__":
    main()