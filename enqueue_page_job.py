import json

import boto3

QUEUE_URL = "https://sqs.us-west-2.amazonaws.com/567434252311/test-convert-pdf-to-html-requests"
USER_ID = "ketan1947"
PDF_ID = "9b037e38-2959-42ef-99d3-443a72275a70"
BUCKET = "test-pdf-to-html-demo-bucket-567434252311-us-west-2-an"
PDF_NAME = "TUTTI_COL_MAT_AZZURRO_SMART_3_Unita1_DivisibilitaPolinomi-1-10"
START_PAGE = 1
END_PAGE = 10

sqs = boto3.client("sqs")

for page in range(START_PAGE, END_PAGE + 1):
    body = {
        "user_id": USER_ID,
        "pdf_id": PDF_ID,
        "page": page,
        "bucket": BUCKET,
        "pdfS3Uri": f"s3://{BUCKET}/{PDF_ID}/pages/{page}/{PDF_NAME}-{page}.pdf",
    }
    response = sqs.send_message(QueueUrl=QUEUE_URL, MessageBody=json.dumps(body))
    print(f"page {page} -> {response['MessageId']}")
