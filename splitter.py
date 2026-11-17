import io
import logging

from botocore.exceptions import ClientError
from pypdf import PdfReader, PdfWriter


class Splitter:
    def __init__(self, s3, dynamodb, table_name):
        self.s3 = s3
        self.dynamodb = dynamodb
        self.table_name = table_name

    def pdf_split(self, pdf_path, bucket, user_id, pdf_id, pdf_name):
        reader = PdfReader(str(pdf_path))
        total = len(reader.pages)
        if not total:
            raise ValueError("PDF contains no pages")
        self.dynamodb.update_item(
            TableName=self.table_name,
            Key={"user_id": {"S": user_id}, "type": {"S": f"PDFDATA#{pdf_id}"}},
            UpdateExpression="SET total_pages = :total",
            ExpressionAttributeValues={":total": {"N": str(total)}},
            ConditionExpression="attribute_exists(user_id) AND attribute_exists(#type)",
            ExpressionAttributeNames={"#type": "type"},
        )
        keys = []
        for page_number, page in enumerate(reader.pages, start=1):
            writer = PdfWriter()
            writer.add_page(page)
            data = io.BytesIO()
            writer.write(data)
            key = f"{pdf_id}/pages/{page_number}/{pdf_name}-{page_number}.pdf"
            self.s3.put_object(Bucket=bucket, Key=key, Body=data.getvalue(),
                               ContentType="application/pdf")
            try:
                self.dynamodb.put_item(
                    TableName=self.table_name,
                    Item={"user_id": {"S": user_id},
                          "type": {"S": f"PDFDATA#{pdf_id}#{page_number}"},
                          "html": {"S": ""}, "css": {"S": ""},
                          "progress": {"S": "processing"}},
                    ConditionExpression="attribute_not_exists(user_id) AND attribute_not_exists(#type)",
                    ExpressionAttributeNames={"#type": "type"},
                )
            except ClientError as error:
                if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
            keys.append(key)
            logging.info("Uploaded page %s to s3://%s/%s", page_number, bucket, key)
        return keys