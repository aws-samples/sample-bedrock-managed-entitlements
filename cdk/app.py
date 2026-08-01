#!/usr/bin/env python3
"""CDK app entry point for MPPO Grants Automation.

IMPORTANT: Deploy this stack in us-east-1.
Marketplace agreement events and License Manager licenses are always in us-east-1.
"""

import aws_cdk as cdk

from mppo_stack import MppoGrantsAutomationStack

app = cdk.App()
MppoGrantsAutomationStack(
    app,
    "MppoGrantsAutomationStack",
    description="Automated MPPO acceptance and org-wide license distribution via License Manager",
    env=cdk.Environment(region="us-east-1"),
)

app.synth()
