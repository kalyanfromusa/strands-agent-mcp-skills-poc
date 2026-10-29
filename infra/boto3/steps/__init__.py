"""One module per resource. Each exposes `deploy(ctx)` and `destroy(ctx)`, plus pure `build_*_request()`
functions that return the exact request dicts sent to AWS (validated offline by tests/test_boto3_shapes.py).

What this folder does
    Each file creates and deletes one kind of AWS resource (S3, Cognito, IAM, Gateway, ...).

Where it fits
    `deploy.py` calls the `deploy` functions in dependency order; `destroy.py` calls the `destroy`
    functions in the reverse order. They share helpers from `common.py`.

Step by step (the pattern every module follows)
    1. `build_*_request()` functions build the request exactly as AWS will receive it. They are "pure"
       (no AWS calls), so the tests can check every request against the official AWS API description
       without an AWS account.
    2. `deploy(ctx)` looks the resource up by name, then creates it, updates it, or leaves it alone.
    3. `destroy(ctx)` deletes it and treats "already gone" as success.

Key terms
    Request dict: the set of named fields sent to one AWS API call, e.g. {"TableName": "shopassist-orders"}.
"""
