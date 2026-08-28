// Shared API types.
//
// Role and Template interfaces were previously re-declared in four separate
// files, so they drifted. They live here now.

export interface SystemStatus {
  db_configured: boolean;
  inference_configured: boolean;
  inference_verified: boolean;
  inference_status: string;
  embedding_configured: boolean;
  embedding_verified: boolean;
  embedding_status: string;
  configured: boolean;
  ready: boolean;
  reasons: string[];
  /** Always "environment": credentials come from a Secret, never from a file. */
  config_source: string;
  /** kubectl command to create the missing Secret, when something is missing. */
  remediation: string | null;
  last_op: LastOperation | null;
}

export interface LastOperation {
  status: "pending" | "success" | "error";
  type: "setup" | "reseed";
  message?: string;
  timestamp?: string;
}

/** Operator-tunable settings. Credentials are deliberately absent. */
export interface SystemSettings {
  id: number;
  created_at: string;
  updated_at: string;
  debug: boolean;
  retrieval_limits_per_agent: number;
  max_evidence_per_message: number;
  default_turn_limit: number;
  cleanup_rules: string;
  inference_temperature: number;
  supervisor_temperature: number;
  supervisor_prompt: string | null;
  agent_prompt: string | null;
}

export type SystemSettingsUpdate = Partial<
  Omit<SystemSettings, "id" | "created_at" | "updated_at">
>;

export interface PromptMetadataItem {
  title: string;
  description: string;
  placeholders: string[];
  default: string;
}

export type PromptMetadata = Record<string, PromptMetadataItem>;

export interface Role {
  id: string;
  display_name: string;
  title: string;
  department: string;
  seniority?: string | null;
  summary?: string | null;
  responsibilities?: string[];
  kpis?: string[];
  priorities?: string[];
  objectives?: string[];
  risk_tolerance?: string | null;
  tone?: string[];
  collaboration_style?: string | null;
  challenge_style?: string | null;
  allowed_shared_library_access?: boolean;
  system_prompt?: string | null;
  /** Tool grant. Resolves server-side to a Kubernetes-enforced profile. */
  default_tools?: string[];
  ui_metadata?: Record<string, unknown>;
}

export interface Template {
  id: string;
  name: string;
  description?: string | null;
  brief?: string | null;
  objective?: string | null;
  expectations?: string | null;
  agenda?: string | null;
  default_selected_attendee_ids?: string[];
  default_document_ids?: string[];
  is_builtin?: boolean;
}

/** What a persona was granted, and what the cluster will enforce. */
export interface AttendeeCapabilities {
  agent_id: string
  display_name: string
  title: string
  profile: string
  profile_description: string
  granted_tools: string[]
  can_execute_code: boolean
  holds_metrics_credential: boolean
}

export interface MeetingCapabilities {
  attendees: AttendeeCapabilities[]
  all_tools: string[]
}

/** One tool call as recorded in the meeting's audit trail. */
export interface ToolAuditEntry {
  agent_id: string
  tool: string | null
  ok: boolean
  denied_reason?: string | null
  duration_ms?: number
  error?: string
}

/** Something an agent produced during the meeting. */
export interface MeetingArtifact {
  id: string
  agent_id: string | null
  kind: string
  title: string
  mime_type: string
  body: string
  created_at: string
}

export interface MeetingActionItem {
  id: string
  text: string
  due: string | null
  raised_by_agent_id: string | null
}

export interface MeetingArtifacts {
  artifacts: MeetingArtifact[]
  action_items: MeetingActionItem[]
}
