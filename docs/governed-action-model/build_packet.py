"""Generate proposal-only model artifacts; never contacts external systems."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def write(name, value):
    (ROOT / name).write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')

def obj(props, required=None):
    return {'type':'object','additionalProperties':False,'properties':props,
            'required':list(props) if required is None else required}

def string(**kw): return {'type':'string',**kw}
def enum(*vals): return {'enum':list(vals)}
def array(items, **kw): return {'type':'array','items':items,**kw}
def ref(name): return {'$ref':f'#/$defs/{name}'}

identifier = string(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')
sha = string(pattern='^[a-f0-9]{64}$')
outcomes = ['passed','failed','approved','declined','completed','blocked','no_op',
            'submission_uncertain','acknowledged','reconciled','cancelled','expired']
effects = ['read','research_write','authority_change','execution_state_write','paper_effect']
roles = ['data_service','research_worker','test_worker','method_reviewer','review_service',
         'operator','campaign_owner','risk_service','broker_gateway','reconciler','projector','learning_agent']

artifact = obj({'id':identifier,'kind':identifier,'sha256':sha,'classification':enum('synthetic_fixture'),
                'payload':{'type':'object'}})
input_binding = {'oneOf':[
 obj({'artifact_id':identifier,'expected_type':identifier}),
 obj({'producer_node_id':identifier,'output_name':identifier,'expected_type':identifier})]}
dep = obj({'node_id':identifier,'on_outcomes':array(enum(*outcomes),minItems=1,uniqueItems=True)})
node = obj({
 'id':identifier, 'label':string(minLength=1,maxLength=160),
 'operation':identifier, 'operation_version':string(pattern=r'^\d+\.\d+$'),
 'owner_role':enum(*roles),'effect_class':enum(*effects),
 'target_environment':enum('research','paper'),
 'dependencies':array(ref('dependency'),uniqueItems=True),
 'inputs':{'type':'object','additionalProperties':ref('input_binding')},
 'outputs':{'type':'object','minProperties':1,'additionalProperties':identifier},
 'required_guards':array(identifier,uniqueItems=True),
 'retry':obj({'mode':enum('bounded_idempotent','no_automatic_retry','reconcile_before_retry'),
              'max_attempts':{'type':'integer','minimum':1,'maximum':8}}),
 'timeout_seconds':{'type':'integer','minimum':1,'maximum':86400},
 'on_failure':enum('block_descendants','preserve_uncertain_effect','retain_receipt'),
 'required_receipts':array(identifier,minItems=1,uniqueItems=True),
 'postcondition':identifier
})
plan_schema = {
 '$schema':'https://json-schema.org/draft/2020-12/schema',
 '$id':'urn:trex:proposal:action-plan:0.1',
 'title':'TREX proposal-only action plan v0.1',
 'description':'Design/model validation only. Passing this schema grants no authority and does not execute a node.',
 **obj({'schema':{'const':'trex-action-plan/0.1'},'artifact_status':{'const':'synthetic_design_example'},
        'execution_authorized':{'const':False},'plan_id':identifier,'revision':{'type':'integer','minimum':1},
        'goal':string(minLength=1),'state':{'const':'proposed'},
        'source_repository_head':string(pattern='^[a-f0-9]{40}$'),
        'artifacts':array(ref('artifact'),minItems=1),
        'nodes':array(ref('node'),minItems=1),
        'non_scheduling_links':array(obj({'from':identifier,'to':identifier,
          'relation':enum('compares_to','supports_review','proposes_revision_of')})),
        'notes':array(string())}),
 '$defs':{'artifact':artifact,'input_binding':input_binding,'dependency':dep,'node':node}
}
write('contracts/action-plan.schema.json',plan_schema)

def artifact_record(name, kind, payload):
    raw = json.dumps(payload, sort_keys=True, separators=(',',':'), ensure_ascii=False, allow_nan=False).encode()
    return {'id':name,'kind':kind,'sha256':hashlib.sha256(raw).hexdigest(),
            'classification':'synthetic_fixture','payload':payload}
arts = [
 artifact_record('fixture.snapshot','DataSnapshot',{'fixture':True,'contains_market_observations':False,
    'purpose':'Contract fixture only; a real loader must resolve an authorized immutable data snapshot.'}),
 artifact_record('fixture.baseline','StrategyVersion',{'fixture':True,'strategy_name':'baseline-example','revision':1}),
 artifact_record('fixture.challenger','StrategyVersion',{'fixture':True,'strategy_name':'cost-aware-variant-example','revision':1}),
 artifact_record('fixture.protocol','ExperimentSpec',{'fixture':True,'scope':'exploratory','evaluation_window':'not_assigned',
    'limits':'operator_to_define','no_live_money':True}),
 artifact_record('fixture.policy','GovernancePolicy',{'fixture':True,'grants':[],
    'default':'deny_effects_without_verified_mandate','promotion':'proposal_only'})
]

def a(name, typ): return {'artifact_id':name,'expected_type':typ}
def p(node, port, typ): return {'producer_node_id':node,'output_name':port,'expected_type':typ}
def d(node, *on): return {'node_id':node,'on_outcomes':list(on or ('completed',))}
def n(id, label, operation, role, effect, deps, inputs, outputs, guards=(),
      retry='bounded_idempotent', outcome='receipt_published', env='research'):
    return {'id':id,'label':label,'operation':operation,'operation_version':'0.1','owner_role':role,
       'effect_class':effect,'target_environment':env,'dependencies':deps,'inputs':inputs,'outputs':outputs,
       'required_guards':list(guards),'retry':{'mode':retry,'max_attempts':3 if retry=='bounded_idempotent' else 1},
       'timeout_seconds':300,'on_failure':'preserve_uncertain_effect' if effect=='paper_effect' else 'block_descendants',
       'required_receipts':['input_manifest','operation_outcome'], 'postcondition':outcome}

nodes = [
 n('N01','Resolve pinned research inputs','research.resolve_inputs','data_service','read',[],
   {'source':a('fixture.snapshot','DataSnapshot')},{'snapshot':'DataSnapshot'},['authorized_data_scope','input_hash_match']),
 n('N02','Check data capability and timing','research.check_coverage','data_service','research_write',[d('N01')],
   {'snapshot':p('N01','snapshot','DataSnapshot')},{'coverage':'CoverageReceipt'},['point_in_time_valid']),
 n('N03','Replay frozen baseline','research.run_baseline','research_worker','research_write',[d('N02','passed')],
   {'snapshot':p('N01','snapshot','DataSnapshot'),'strategy':a('fixture.baseline','StrategyVersion'),
    'protocol':a('fixture.protocol','ExperimentSpec')},{'result':'ResearchResult'},['research_budget_available']),
 n('N04','Replay challenger on the same inputs','research.run_challenger','research_worker','research_write',[d('N02','passed')],
   {'snapshot':p('N01','snapshot','DataSnapshot'),'strategy':a('fixture.challenger','StrategyVersion'),
    'protocol':a('fixture.protocol','ExperimentSpec')},{'result':'ResearchResult'},['research_budget_available']),
 n('N05','Build paired comparison','research.compare','research_worker','research_write',[d('N03'),d('N04')],
   {'baseline':p('N03','result','ResearchResult'),'challenger':p('N04','result','ResearchResult')},
   {'comparison':'ComparisonResult'},['same_comparison_basis']),
 n('N06','Publish comparison plot specification','workspace.project_comparison','projector','research_write',[d('N05')],
   {'result':p('N05','comparison','ComparisonResult')},{'chart':'ChartSpec'},['result_hash_match']),
 n('N07','Run deterministic broker-fault acceptance','validation.run_fault_suite','test_worker','research_write',[d('N02','passed')],
   {'protocol':a('fixture.protocol','ExperimentSpec')},{'tests':'TestReceipt'},['no_external_broker_access']),
 n('N08','Review methods and evidence limitations','review.methods','method_reviewer','research_write',[d('N05')],
   {'result':p('N05','comparison','ComparisonResult')},{'review':'MethodReview'},['review_inputs_complete']),
 n('N09','Assemble prospective paper-test readiness','review.paper_readiness','review_service','research_write',[d('N07','passed'),d('N08')],
   {'tests':p('N07','tests','TestReceipt'),'methods':p('N08','review','MethodReview')},
   {'readiness':'ReadinessReview'},['reviewer_conflicts_disclosed']),
 n('N10','Propose a bounded paper mandate','paper.propose_mandate','learning_agent','research_write',[d('N09','passed')],
   {'review':p('N09','readiness','ReadinessReview'),'policy':a('fixture.policy','GovernancePolicy')},
   {'proposal':'MandateProposal'},['no_authority_in_proposal']),
 n('N11','Approve or decline the exact mandate proposal','governance.decide_mandate','operator','authority_change',[d('N10')],
   {'proposal':p('N10','proposal','MandateProposal')},{'mandate':'PaperMandate'},
   ['authenticated_operator','exact_proposal_hash','explicit_paper_scope'],retry='no_automatic_retry',outcome='authority_decision_recorded'),
 n('N12','Instantiate one authorized paper decision cycle','campaign.instantiate_cycle','campaign_owner','execution_state_write',[d('N11','approved')],
   {'mandate':p('N11','mandate','PaperMandate')},{'draft':'OrderIntentDraft'},
   ['mandate_active','current_owner','current_strategy_revision'],env='paper'),
 n('N13','Recheck scope and atomically reserve risk','paper.reserve_and_permit','risk_service','execution_state_write',[d('N12')],
   {'draft':p('N12','draft','OrderIntentDraft'),'mandate':p('N11','mandate','PaperMandate')},
   {'permit':'EffectPermit','reservation':'RiskReservation','intent':'OrderIntent'},
   ['mandate_active','account_bound','current_owner','fresh_risk_snapshot','fresh_quotes','capacity_available','effect_hash_bound'],env='paper'),
 n('N14','Submit through the exclusive paper gateway','paper.submit','broker_gateway','paper_effect',[d('N13')],
   {'permit':p('N13','permit','EffectPermit'),'reservation':p('N13','reservation','RiskReservation'),
    'intent':p('N13','intent','OrderIntent')},{'submission':'SubmissionReceipt'},
   ['mandate_active','permit_active','account_bound','current_owner','fresh_risk_snapshot','fresh_quotes','effect_hash_bound'],
   retry='reconcile_before_retry',outcome='submission_observed_or_uncertain',env='paper'),
 n('N15','Reconcile acknowledgment, executions and exposure','paper.reconcile','reconciler','execution_state_write',
   [d('N14','acknowledged','submission_uncertain')],{'submission':p('N14','submission','SubmissionReceipt')},
   {'snapshot':'ReconciledAccountSnapshot'},['account_bound','current_owner'],env='paper'),
 n('N16','Publish the observed paper-result view','paper.project_result','projector','research_write',[d('N15','reconciled')],
   {'snapshot':p('N15','snapshot','ReconciledAccountSnapshot')},{'result':'PaperOutcome'},['coverage_disclosed']),
 n('N17','Review observed versus expected outcomes','review.outcomes','review_service','research_write',[d('N16'),d('N05')],
   {'observed':p('N16','result','PaperOutcome'),'modeled':p('N05','comparison','ComparisonResult')},
   {'review':'OutcomeReview'},['evidence_kinds_separate','labels_mature']),
 n('N18','Propose a new version, without activating it','adaptation.propose_revision','learning_agent','research_write',[d('N17')],
   {'review':p('N17','review','OutcomeReview'),'baseline':a('fixture.baseline','StrategyVersion')},
   {'proposal':'AdaptationProposal'},['adaptation_budget_available','no_authority_in_proposal'])
]
# Type/operation catalog is also only a fixture: production ownership comes from trusted server registration.
registry = {'schema':'trex-operation-registry-fixture/0.1','status':'synthetic_fixture_not_authority',
 'operations':[{k:x[k] for k in ('operation','operation_version','owner_role','effect_class','target_environment','outputs')}
               | {'input_types':{k:v['expected_type'] for k,v in x['inputs'].items()}} for x in nodes]}
write('examples/operation-registry.fixture.json',registry)
plan = {'schema':'trex-action-plan/0.1','artifact_status':'synthetic_design_example','execution_authorized':False,
 'plan_id':'example.research-to-paper','revision':1,'goal':'Compare, review, propose a bounded paper experiment, observe and propose a next version.',
 'state':'proposed','source_repository_head':'c4035f6b30f5a177c0a9b4334b682589c9d62377',
 'artifacts':arts,'nodes':nodes,'non_scheduling_links':[
   {'from':'N04','to':'N03','relation':'compares_to'},
   {'from':'N18','to':'N03','relation':'proposes_revision_of'}],
 'notes':['No node has run. Output references are declared ports, not existing evidence.',
   'No actual dataset, funded result, broker identifier, policy threshold or active grant is supplied.',
   'N09 passes operational/method readiness for an exploratory trial, not a profitability threshold.',
   'N12-N16 represent one bounded child cycle. A durable campaign instantiates new cycle and effect identities, not a back-edge to N14.',
   'The fixture registry is not trusted runtime policy. The validator checks structural conformance only.']}
write('examples/research-to-paper.plan.json',plan)

# Illustrative domain-record field contracts. These are design records, not a broad claim of generated runtime models.
records = {
 'ArtifactRef':['object_id','revision','content_sha256','schema','classification','access_scope'],
 'PlanRevision':['plan_id','revision','parent_revision','goal_ref','nodes','dependencies','input_refs','contract_version'],
 'PlanRun':['run_id','plan_revision_ref','resolved_inputs_manifest','engine_release_ref','status','started_at','closed_at'],
 'ActionAttempt':['attempt_id','logical_node_id','run_id','attempt_number','actor_ref','operation_contract_ref','input_refs','lifecycle_state','outcome_refs'],
 'GovernanceDecision':['decision_id','subject_principal','action_digest','policy_ref','mandate_ref','account_epoch','decision','reason_codes','issued_at','expires_at','obligations','trusted_issuer'],
 'PaperMandate':['mandate_id','revision','principal','account_binding_ref','strategy_revision_allowlist','parameter_envelope_ref','risk_budget_ref','operation_allowlist','valid_from','expires_at','revocation_epoch','protective_policy_ref','operator_decision_ref'],
 'EffectPermit':['permit_id','logical_effect_id','intent_sha256','mandate_revision_ref','policy_ref','owner_epoch','reservation_ref','account_revision','quote_snapshot_ref','issued_at','expires_at','single_use_state'],
 'BrokerObservation':['observation_id','account_binding_ref','broker_ids','effective_at','received_at','raw_payload_ref','correction_of','completeness'],
 'MetricObservation':['metric_id','result_ref','definition_version','value_decimal_or_null','unit','capital_basis','effective_at','knowledge_cutoff','coverage','missing_reason','source_row_refs'],
 'ChartSpec':['chart_id','revision','series_bindings','shared_time_domain','metric_basis','evidence_labels','missingness_rule','transform_chain','interval_semantics','point_locator','display_reduction','authoritative_result_refs'],
 'AdaptationPolicy':['id','revision','permitted_parameter_space','learning_algorithm_ref','training_scope','evaluation_protocol_ref','trigger_rules','cooldown_rule','turnover_and_risk_bounds','freeze_conditions','fallback_policy_ref','change_authority_ref'],
 'AdaptationProposal':['id','parent_strategy_ref','proposed_strategy_ref','input_evidence_refs','observed_failures','uncertainty','out_of_sample_receipts','scope_diff','affected_descendants','requested_commitment'],
 'ReviewPacket':['id','subject_revision_ref','engineering_receipts','statistical_assessment','operational_readiness','scope_limitations','findings','dispositions','reviewer_refs','authority_request_ref'],
 'StreamEnvelope':['stream_id','producer_epoch','sequence','entity_revision','correlation_id','causation_id','effective_at','recorded_at','schema','payload_ref','projection_watermark']
}
write('contracts/domain-records.json',{'schema':'trex-domain-record-outline/0.1','status':'proposed_field_contracts_not_runtime_schema',
 'records':records,'note':'Action-plan.schema.json is the schema-tested subset. These records require detailed generated Python/TypeScript contracts in TREX.'})

case_rows = [
 ('GOV-01','Forged client approval','No permit or effect; authoritative identity and policy required.'),
 ('GOV-02','Mandate revoked after proposal','Dispatch refuses new exposure and records current revocation epoch.'),
 ('GOV-03','Plan/strategy changes after approval','Approval does not cover changed material; new review or within-envelope proof required.'),
 ('GOV-04','Stale owner lease','Exclusive gateway refuses old owner; no bypass connection retains send ability.'),
 ('GOV-05','Two requests consume remaining capacity','Only feasible reservations commit; pending and unknown effects still count.'),
 ('GOV-06','Sealed-data read or prompt injection','No protected read/export/effect authority obtained from source text or an LLM.'),
 ('EXE-01','Crash before send','Committed intent recovered; dedupe before any permitted dispatch.'),
 ('EXE-02','Crash after send before acknowledgment','Keep uncertainty and reservation; reconcile before retry.'),
 ('EXE-03','Partial fill/cancel/replacement race','Only reconciled positive remainder may be sent; uncertainty not cancelled.'),
 ('EXE-04','Late execution/price/commission correction','Append correction and update derived results with revisions; retain original decision context.'),
 ('EXE-05','Terminal state save failure','Do not report successful terminal completion from unsaved memory.'),
 ('EXE-06','Mandate expiry while position open','No new exposure; preauthorized protection/reconciliation follows explicit surviving scope.'),
 ('EXE-07','LLM/browser/resource outage','Deterministic control and recovery remain independent; protective work has capacity.'),
 ('EXE-08','Manual order/assignment/account reset','No silent attribution; reconcile or create new epoch and expose actual quantities.'),
 ('EVI-01','Payload tamper or source mismatch','Integrity check rejects mismatch; old result kept, affected current claim flagged.'),
 ('EVI-02','Legitimate append after verification','Verification succeeds without stale-head false failure.'),
 ('EVI-03','Same request changed dependencies','New execution identity; no stale cache result under old request hash.'),
 ('EVI-04','Conflicting providers or revised evidence','Retain each source and disposition; no overwrite or averaging away disagreement.'),
 ('ACC-01','Buy/hold/sell with fees and external flows','Independent cash+inventory NAV agrees; costs and flows counted once.'),
 ('ACC-02','Unpriced position','Known exposure remains visible; affected aggregate metrics null with reasons.'),
 ('ADA-01','New model fits observed winners','Retain trial history; cannot relabel explored data as untouched validation.'),
 ('ADA-02','Adaptive version selection over time','Replay the selector and historical information state, not hindsight-spliced winner curves.'),
 ('ADA-03','Distribution shift or missing inputs','Log diagnosis uncertainty; bounded fallback or freeze; no automatic risk increase.'),
 ('ADA-04','Changing strategy with open inventory','Old position exit/owner policy persists unless explicit migration is authorized.'),
 ('ADA-05','Paper outcomes overstate modeled execution quality','Keep simulator/model/real evidence separate; no inferred live execution validation.'),
 ('UI-01','Old voice referent after selection changes','Original target/revision retained or explicit conflict; no silent retargeting.'),
 ('UI-02','Slow result after human edit','Revision/generation fence preserves new accepted state.'),
 ('UI-03','Graph drag or chart zoom','Presentation-only unless explicit domain edit; no broker grant or study mutation.'),
 ('UI-04','Chart point/table/export disagreement','All bind same immutable metric/result; stop unsupported presentation.'),
 ('UI-05','Stream gap/reconnect','Fresh authoritative snapshot and continuation; no false complete/flat status.'),
 ('UI-06','Mobile/keyboard only episode','Compare, inspect, propose, review, intervene and resume without voice/drag.'),
 ('REV-01','Review skipped or outcome indeterminate','Not counted as passed; explicitly scoped readiness and reason.'),
 ('REV-02','Two reviewers share the same evidence/error','Do not treat model vote count as independent statistical evidence.'),
 ('REC-01','Cold restore while broker state advanced','Local integrity followed by broker reconciliation before effects resume.')
]
write('acceptance-cases.json',{'schema':'trex-system-acceptance-plan/0.1','status':'required_future_tests_not_run_by_this_packet',
 'cases':[{'id':i,'scenario':s,'expected':e} for i,s,e in case_rows]})

work = [
 ('GRAPH-01',['CORE-01','CORE-03','AGENT-02'],'Typed graph and revision contracts','Plan, attempt, effect, result and authority IDs remain distinct.'),
 ('GRAPH-02',['CORE-03','DATA-02','OPS-02'],'Provenance projection and snapshot custody','Trace from a result point to actual source revisions; no duplicate accounting store.'),
 ('GRAPH-03',['PAPER-01','PAPER-02','PAPER-04','OPS-01'],'Mandate evaluation and effect permits','Deny by default; bound current account, input, owner and policy state at dispatch.'),
 ('GRAPH-04',['TEST-02','PAPER-02','PAPER-03','TEST-01'],'Durable action-plan runner','Bounded scheduling, nonempty receipts, idempotent compute and reconciled effects.'),
 ('GRAPH-05',['TEST-03','RL-02','RL-05','ADV-02'],'Adaptive evaluation and version promotion','Frozen champion, versioned challenger, full selector replay and scoped review.'),
 ('GRAPH-06',['GUI-01','GUI-02','GUI-03','AGENT-01','GUI-04'],'Plan/actual/review/chart linked workspace','One object/revision across all lenses; stale result and permission fences.'),
 ('GRAPH-07',['PAPER-05','PAPER-06','GUI-05','OPS-02'],'Supervised paper-cycle acceptance','Separate operator-authorized canary; no waiver of SP-3/SP-5.')
]
write('integration-map.json',{'schema':'trex-graph-integration-map/0.1','status':'proposed_slices_not_new_issue_ids',
 'prior_backlog':'TREX-Integrated-System-Backlog-20260926.json',
 'current_head_observed':'c4035f6b30f5a177c0a9b4334b682589c9d62377',
 'delta':'Forecast evaluation machinery now exists; calibration explicitly not claimed. Do not restart RL-3 from scratch.',
 'slices':[{'id':i,'extends_existing_tasks':p,'title':t,'acceptance':a} for i,p,t,a in work]})
