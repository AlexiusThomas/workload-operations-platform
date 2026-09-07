Placeholder asset for the wop-idempotency-layer.

CDK requires Lambda layers to reference an on-disk asset (inline code is not
permitted for layers). The real idempotency layer payload (backend idempotency
module) is packaged at deploy time; this directory exists only so `cdk synth`
can produce a valid template without bundling. Do not add runtime code here.