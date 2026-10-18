# The instance's role: Session Manager only (a shell without SSH or an open port). rrotor calls no AWS API.

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "rrotor" {
  name_prefix        = "${var.name}-"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.rrotor.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "rrotor" {
  name_prefix = "${var.name}-"
  role        = aws_iam_role.rrotor.name
  tags        = local.tags
}
