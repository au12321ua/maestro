// Web types for the /api/v1 contract: aliases of the generated `schema.d.ts`
// (`npm run gen:api`, from ../openapi/maestro-v1.json), so a changed API that was not
// regenerated fails `npm run check:api` and the type check. Nothing here is hand-written
// structure: every response field has a concrete schema in the contract.
import type { components } from "./schema";

type S = components["schemas"];

export type Role = S["User"]["role"];
export type Scope = S["Pat"]["scope"];

export type FieldErrorModel = S["FieldErrorModel"];
export type ErrorBody = S["ErrorBody"];
/** An error as the client hands it to pages: the client fills in field_errors when absent. */
export type ApiErrorBody = ErrorBody & { field_errors: FieldErrorModel[] };
export type Meta = S["Meta"];

export type User = S["User"];
export type Me = S["Me"];
export type SessionInfo = S["SessionInfo"];
export type SetupStatus = S["SetupStatus"];

export type Invitation = S["Invitation"];
export type IssuedInvitation = S["IssuedInvitation"];
export type InvitationInfo = S["InvitationInfo"];

export type Pat = S["Pat"];
export type IssuedPat = S["IssuedPat"];

export type Credential = S["Credential"];
export type CredentialKind = Credential["kind"];
export type CredentialName = S["CredentialName"];
export type MasterKeyStatus = S["MasterKeyStatus"];

export type DatasetVersion = S["DatasetVersion"];
export type Dataset = S["Dataset"];
export type DeletionState = S["DeletionState"];
export type DatasetVersionDetail = S["DatasetVersionDetail"];

export type LocalPreparation = S["LocalPreparation"];
export type AuthorParameter = S["AuthorParameter"];
export type TaskBlocker = S["TaskBlocker"];
export type Task = S["Task"];

/** A discriminated union on `kind`; narrow on it to read progress and result. */
export type Operation = S["Operation"];
export type Preparation = S["Preparation"];

export type SourceDataset = S["SourceDataset"];
export type SourceVersion = S["SourceVersion"];
export type SourceVersionDetail = S["SourceVersionDetail"];

export type ExecutionMode = S["ConfigRequest-Input"]["execution_mode"] & string;
export type ParameterValue = string | S["CredentialRef"];

/** A run configuration request (03) as sent: unset fields are inherited from the layer below. */
export type ConfigRequest = S["ConfigRequest-Input"];
/** The same request as the service returns it (GET /runs/{run_id}/request). */
export type ConfigRequestOutput = S["ConfigRequest-Output"];
export type RunCreate = S["RunCreate"];
export type RunRerun = S["RunRerun"];

export type Plan = S["Plan"];

export type Batch = S["Batch"];
export type BatchProgress = S["BatchProgress"];
export type BatchAccepted = S["BatchAccepted"];
export type BatchCreate = S["BatchCreate"];
export type BatchPreview = S["BatchPreview"];

export type ConfigBlocker = S["ConfigBlocker"];
export type FieldSource = S["FieldSource"];
export type EffectiveAttachment = S["EffectiveAttachment"];
export type EffectiveTask = S["EffectiveTask"];
export type ExcludedTask = S["ExcludedTask"];
export type CacheObservation = S["CacheObservation"];
export type ConfigResolution = S["ConfigResolution"];

export type ModelProtocol = S["ModelConfig"]["protocol"];
export type ModelConfig = S["ModelConfig"];

export type HarnessParameterSpec = S["HarnessParameterSpec"];
export type Harness = S["Harness"];
export type HarnessAdapter = S["HarnessAdapter"];
export type UpstreamVersions = S["UpstreamVersions"];

export type AttachmentFile = S["AttachmentFile"];
/** A published attachment snapshot (03 section 6); its content never changes. */
export type Attachment = S["Attachment"];
export type AttachmentDiff = S["AttachmentDiff"];

export type PolicySetting = S["PolicySetting"];
export type TeamPolicy = S["TeamPolicy"];
export type SetupNetwork = S["SetupNetwork"];

export type RunAccepted = S["RunAccepted"];

export type RunScoring = S["RunScoring"];
export type ScoringContract = S["ScoringContract"];
export type TaskSummary = S["TaskSummary"];
export type RunSummary = S["RunSummary"];
export type RunCounts = S["RunCounts"];
export type Run = S["Run"];
export type RunSnapshot = S["RunSnapshot"];
export type RunDetail = S["RunDetail"];

export type TrialResult = S["TrialResult"];
export type AttemptStep = S["AttemptStep"];
export type Attempt = S["Attempt"];
export type Trial = S["Trial"];
export type Artifact = S["Artifact"];

export type ExportRequest = S["ExportRequest"];
export type Export = S["Export"];

export type RecycleEntry = S["RecycleEntry"];
export type RecycleObjectType = RecycleEntry["object_type"];
export type AuditEvent = S["AuditEvent"];

export type Capabilities = S["Capabilities"];
export type CapacityOverview = S["CapacityOverview"];
export type Health = S["Health"];
export type SystemStatus = S["SystemStatus"];
/** A plan template as sent: any fields of a configuration request. */
export type PlanTemplate = S["PlanTemplate-Input"];
/** Any subset of a configuration request as the service returns it (a plan's template, a
 *  run's request): the loosest shape, for filling the new-run form. */
export type PartialRequest = S["PlanTemplate-Output"];

/** Runs side by side (06 section 8), computed on demand; a snapshot holds a frozen one. */
export type Comparison = S["Comparison"];
export type ComparedRunRow = S["ComparedRunRow"];
export type ComparedTask = S["ComparedTask"];
export type ComparisonCell = S["ComparisonCell"];
export type ComparisonSettings = S["ComparisonSettings"];
export type ComparisonSettingsInput = S["ComparisonSettingsInput"];
export type ComparisonLayout = ComparisonSettings["layout"];
export type Dimension = S["ComparisonDimension"]["key"];
export type FailureGroup = S["FailureGroupCount"]["key"];
export type Collection = S["Collection"];
export type CollectionMember = S["CollectionMember"];
export type CollectionEvent = S["CollectionEvent"];
export type Scenario = Collection["scenario"];
export type VariableDimension = Collection["variable_dimensions"][number];
export type Snapshot = S["Snapshot"];
export type SnapshotSummary = S["SnapshotSummary"];
export type SnapshotNote = S["SnapshotNote"];
