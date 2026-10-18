# The instance's own service account: logs and metrics only. rrotor calls no Google Cloud API.

resource "google_service_account" "rrotor" {
  project      = var.project
  account_id   = var.name
  display_name = "rrotor (${var.name})"
}

resource "google_project_iam_member" "rrotor" {
  for_each = toset(["roles/logging.logWriter", "roles/monitoring.metricWriter"])
  project  = var.project
  role     = each.value
  member   = "serviceAccount:${google_service_account.rrotor.email}"
}
