variable "name_prefix" {
  description = "Environment name prefix, e.g. shopassist-prod."
  type        = string
}

variable "event_expiry_days" {
  description = "Days before short-term memory events expire (service range 7 to 365)."
  type        = number
  default     = 30
}
