import asyncio
import random
import uuid
from typing import Any

import structlog
from sqlalchemy import func, select

from app.core.database import require_session_maker
from app.models.documents import Document, DocumentChunk
from app.models.meetings import MeetingTemplate
from app.models.roles import RoleAgent
from app.orchestration import profiles
from app.services.embedding_service import generate_embeddings

logger = structlog.get_logger(__name__)

# Persona traits were drawn from an unseeded RNG, so every install produced
# agents that behaved differently and demos were not reproducible. A fixed
# seed keeps the variety while making it deterministic.
SEED_RNG = random.Random(20260401)


# Persona depth, per role.
#
# The seeded personas used to share one templated summary and formulaic
# priorities, and carried no responsibilities, KPIs or objectives at all --
# so the fields the prompt interpolates rendered empty even once the prompt
# was fixed. These give each attendee something specific to be, and something
# specific to look up.
PERSONA_DEPTH = {
    "VP Sales": {
        "summary": "Owns revenue and the customer commitments already made. Knows which deals a delay puts at risk.",
        "responsibilities": [
            "Report pipeline and commitments at risk",
            "Assess customer and channel impact of a hold",
            "Own the message to accounts",
        ],
        "kpis": ["Bookings", "Win rate", "Churn in affected accounts"],
        "objectives": ["Keep revenue impact visible in the decision"],
        "priorities": ["Customer commitments", "Revenue continuity", "Honest messaging"],
        "guidance": "Bring the actual pipeline numbers rather than an impression of them -- look them up. Be specific about which commitments break under each option, and do not promise a message you cannot stand behind.",
    },
    "Chief Executive Officer": {
        "summary": "Runs the company. Owns the decision when the room cannot reach one, and owns the consequences either way.",
        "responsibilities": [
            "Make the final call and say plainly what it is",
            "Weigh brand and regulatory exposure against cost",
            "Hold the meeting to a decision rather than a discussion",
        ],
        "kpis": ["Enterprise value", "Time to decision on escalated risks", "Regulatory findings"],
        "objectives": ["Leave with a decision and an owner for every action"],
        "priorities": ["Protect the brand", "Decide, do not defer", "Keep the company solvent"],
        "guidance": "Push for a decision. If people are circling, name the options and pick one. Ask for the number or the precedent when a claim is asserted without either -- and when you need one yourself, look it up rather than asking someone else to.",
    },
    "Finance Director": {
        "summary": "Owns the numbers and the disclosure risk that comes with them. Distrusts any figure that has not been sourced.",
        "responsibilities": [
            "Quantify the financial exposure of each option",
            "Judge what must be disclosed and when",
            "Model the cost of containment against the cost of recall",
        ],
        "kpis": ["Forecast accuracy", "Cost of quality failures", "Audit findings"],
        "objectives": ["Put a defensible number on every option before the room votes"],
        "priorities": ["Disclosure accuracy", "Margin protection", "No surprises to the board"],
        "guidance": "Never assert a figure you have not established. Query the business metrics or read the document; if the number does not exist yet, compute it. Say explicitly when an estimate is an estimate.",
    },
    "General Counsel": {
        "summary": "The company's lawyer. Reads every proposal for what it commits the company to.",
        "responsibilities": [
            "Identify regulatory and contractual obligations",
            "Check proposed communications against policy",
            "Flag anything that creates admission or liability",
        ],
        "kpis": ["Regulatory findings", "Litigation exposure", "Time to legal sign-off"],
        "objectives": ["Ensure the chosen path survives a regulator reading it back"],
        "priorities": ["Regulatory compliance", "Limit liability", "Preserve privilege"],
        "guidance": "Check the actual policy text before you opine; do not rely on recollection. State obligations as obligations and judgement calls as judgement calls, and be explicit about which is which.",
    },
    "Quality Manager": {
        "summary": "Owns the defect investigation. The person who knows whether the root cause is actually understood.",
        "responsibilities": [
            "Establish root cause and say how confident you are",
            "Define the containment boundary -- which units, which dates",
            "Judge whether the failure is systemic or isolated",
        ],
        "kpis": ["Defect escape rate", "Time to root cause", "Recall scope accuracy"],
        "objectives": ["Give the room a defensible containment boundary"],
        "priorities": ["Customer safety", "Root cause over symptom", "Accurate scope"],
        "guidance": "Pull the incident report and the inspection records before characterising the defect. Distinguish what has been confirmed from what is still suspected -- a scope built on a guess is worse than no scope.",
    },
    "QA Inspector": {
        "summary": "Inspects the product itself. Reports what was actually observed on the line, not what should have happened.",
        "responsibilities": [
            "Report inspection findings and sample sizes",
            "Identify which lots and dates are affected",
            "Escalate when process deviates from spec",
        ],
        "kpis": ["Inspection coverage", "False-pass rate", "Deviation reports filed"],
        "objectives": ["Ground the discussion in what was measured"],
        "priorities": ["Accuracy of observation", "Traceability", "Speed of escalation"],
        "guidance": "Speak from records. Cite the lot, the date, the sample size. If someone characterises the defect rate differently from the inspection data, say so.",
    },
    "Manufacturing Operations Manager": {
        "summary": "Runs the plants. Knows what a shipping hold actually costs in idle lines and missed commitments.",
        "responsibilities": [
            "Assess operational feasibility of a hold or recall",
            "Report capacity and rework capability",
            "Sequence the restart once containment is set",
        ],
        "kpis": ["OEE", "On-time delivery", "Rework cost"],
        "objectives": ["Say what each containment option costs in operational terms"],
        "priorities": ["Line continuity", "Realistic commitments", "Worker safety"],
        "guidance": "Be concrete about capacity and timing. If a proposed containment cannot be executed in the time being discussed, say so immediately rather than after the decision.",
    },
    "Production Manager": {
        "summary": "Owns the production schedule and the trade-offs inside it.",
        "responsibilities": [
            "Report schedule impact of a hold",
            "Identify affected work orders",
            "Plan rework sequencing",
        ],
        "kpis": ["Schedule adherence", "Units held", "Rework throughput"],
        "objectives": ["Translate the containment decision into a schedule"],
        "priorities": ["Schedule integrity", "Clear priorities for the floor"],
        "guidance": "Keep the discussion tied to specific work orders and dates. Ask for the containment boundary if it has not been stated precisely enough to act on.",
    },
    "Supply Chain Manager": {
        "summary": "Owns suppliers and inventory. Often the first to know whether a defect came in through the door.",
        "responsibilities": [
            "Trace affected components to suppliers and lots",
            "Assess supplier corrective action",
            "Report inventory exposure in the field and in transit",
        ],
        "kpis": ["Supplier defect rate", "Inventory exposure", "Lead time"],
        "objectives": ["Establish whether the root cause is upstream"],
        "priorities": ["Traceability", "Supplier accountability", "Continuity of supply"],
        "guidance": "Look up the supplier and lot history before attributing cause. Distinguish units in the field from units still in inventory -- the containment options differ completely.",
    },
    "Chief Architect": {
        "summary": "Owns the technical direction and whether a proposal is actually buildable as described.",
        "responsibilities": [
            "Judge technical feasibility and its timeline",
            "Identify architectural risk in proposals",
            "Keep decisions consistent with the platform direction",
        ],
        "kpis": ["Delivery predictability", "Incident rate", "Technical debt"],
        "objectives": ["Ensure the decision is technically executable"],
        "priorities": ["Feasibility over ambition", "Consistency", "Operability"],
        "guidance": "Check prior decisions and precedent before agreeing that something is new. Say plainly when a timeline is not achievable, and what would make it achievable.",
    },
    "Solution Architect": {
        "summary": "Designs the specific solution and knows where it will strain.",
        "responsibilities": [
            "Propose concrete designs against the objective",
            "Surface integration and migration risk",
            "Estimate effort honestly",
        ],
        "kpis": ["Design rework rate", "Integration defects", "Estimate accuracy"],
        "objectives": ["Turn the objective into a design the room can evaluate"],
        "priorities": ["Simplicity", "Operability", "Honest estimates"],
        "guidance": "Ground proposals in what has already been decided; search the corpus or the document library rather than reinventing an approach the company has already rejected.",
    },
    "ML Engineer": {
        "summary": "Builds and runs the models. Knows what the data actually supports.",
        "responsibilities": [
            "Assess whether the data supports a proposed claim",
            "Report model performance honestly, including failure modes",
            "Identify data quality problems early",
        ],
        "kpis": ["Model accuracy in production", "Data quality incidents", "Time to retrain"],
        "objectives": ["Keep claims about the data within what the data shows"],
        "priorities": ["Evidence over enthusiasm", "Reproducibility", "Data quality"],
        "guidance": "Distinguish a measured result from an expected one. If a claim about the data can be checked, check it before agreeing with it.",
    },
    "Systems Administrator": {
        "summary": "Keeps the systems running and knows what the logs say.",
        "responsibilities": [
            "Report system state and incident history",
            "Assess operational risk of proposed changes",
            "Own access, backup and recovery",
        ],
        "kpis": ["Uptime", "Mean time to recovery", "Change failure rate"],
        "objectives": ["Keep the decision operationally survivable"],
        "priorities": ["Availability", "Recoverability", "Least privilege"],
        "guidance": "Speak from records rather than impression. If a change is proposed without a rollback, say so.",
    },
    "Customer Success Manager": {
        "summary": "Carries what customers are actually experiencing into the room.",
        "responsibilities": [
            "Report customer impact and sentiment",
            "Judge how a communication will land",
            "Escalate accounts at risk",
        ],
        "kpis": ["Churn", "NPS", "Escalation volume"],
        "objectives": ["Make sure the customer view is represented in the decision"],
        "priorities": ["Customer trust", "Clear communication", "Retention"],
        "guidance": "Bring specifics -- which accounts, what they said. Push back when an internally comfortable message would read badly to a customer.",
    },
}


async def seed_data() -> None:
    # Behavioral trait pools
    tones_pool = [
        "Professional",
        "Authoritative",
        "Data-driven",
        "Supportive",
        "Direct",
        "Visionary",
        "Pragmatic",
        "Analytical",
        "Enthusiastic",
        "Diplomatic",
        "Critical",
        "Inspirational",
    ]
    collab_pool = [
        "Direct",
        "Consultative",
        "Democratic",
        "Collaborative",
        "Individualistic",
        "Instructional",
    ]
    challenge_pool = [
        "Analytical",
        "Provocative",
        "Constructive",
        "Skeptical",
        "Questioning",
        "Supportive",
    ]

    async with require_session_maker()() as session:
        # 2. Personas
        stmt = select(func.count()).select_from(RoleAgent)
        res = await session.execute(stmt)
        if res.scalar() == 0:
            roles_data: list[dict[str, str]] = [
                {
                    "name": "Chief Executive Officer",
                    "title": "CEO",
                    "dept": "Executive",
                    "seniority": "Executive",
                },
                {
                    "name": "Finance Director",
                    "title": "FD",
                    "dept": "Finance",
                    "seniority": "Executive",
                },
                {
                    "name": "General Counsel",
                    "title": "GC",
                    "dept": "Legal",
                    "seniority": "Executive",
                },
                {
                    "name": "Manufacturing Operations Manager",
                    "title": "Manager",
                    "dept": "Manufacturing",
                    "seniority": "Senior",
                },
                {
                    "name": "Production Manager",
                    "title": "Manager",
                    "dept": "Production",
                    "seniority": "Senior",
                },
                {
                    "name": "Supply Chain Manager",
                    "title": "Manager",
                    "dept": "Supply Chain",
                    "seniority": "Senior",
                },
                {
                    "name": "Quality Manager",
                    "title": "Manager",
                    "dept": "Quality",
                    "seniority": "Senior",
                },
                {
                    "name": "QA Inspector",
                    "title": "Inspector",
                    "dept": "Quality",
                    "seniority": "Mid-Level",
                },
                {
                    "name": "Chief Architect",
                    "title": "Architect",
                    "dept": "IT",
                    "seniority": "Senior",
                },
                {
                    "name": "Solution Architect",
                    "title": "Architect",
                    "dept": "IT",
                    "seniority": "Senior",
                },
                {
                    "name": "ML Engineer",
                    "title": "Engineer",
                    "dept": "IT",
                    "seniority": "Mid-Level",
                },
                {
                    "name": "Systems Administrator",
                    "title": "Admin",
                    "dept": "IT",
                    "seniority": "Mid-Level",
                },
                {"name": "VP Sales", "title": "VP", "dept": "Sales", "seniority": "Senior"},
                {
                    "name": "Customer Success Manager",
                    "title": "Manager",
                    "dept": "Success",
                    "seniority": "Mid-Level",
                },
            ]

            role_ids: list[uuid.UUID] = []
            gc_agent_id = None
            for r in roles_data:
                depth = PERSONA_DEPTH.get(r["name"], {})
                role = RoleAgent(
                    display_name=r["name"],
                    title=r["title"],
                    department=r["dept"],
                    seniority=r.get("seniority", "Senior"),
                    summary=depth.get(
                        "summary",
                        f"The {r['name']} responsible for {r['dept']} strategy and execution.",
                    ),
                    responsibilities=depth.get("responsibilities", []),
                    kpis=depth.get("kpis", []),
                    objectives=depth.get("objectives", []),
                    priorities=depth.get(
                        "priorities", [f"Optimize {r['dept']}", "Reduce risk", "Drive value"]
                    ),
                    risk_tolerance="Low"
                    if "Legal" in r["dept"] or "Quality" in r["dept"]
                    else "Medium",
                    tone=SEED_RNG.sample(tones_pool, SEED_RNG.randint(2, 3)),
                    collaboration_style=SEED_RNG.choice(collab_pool),
                    challenge_style=SEED_RNG.choice(challenge_pool),
                    # The tool grant. Resolution maps this to a capability
                    # profile, which decides the SandboxTemplate, ServiceAccount
                    # and NetworkPolicy the persona's sandbox is built from.
                    default_tools=sorted(profiles.for_persona(r["title"]).tools),
                    # Persona guidance, carried inside the structured prompt.
                    # This is not the template: a persona's notes replacing the
                    # template is what kept every other field from the model.
                    system_prompt=depth.get(
                        "guidance",
                        "Always represent your department's interests strongly. "
                        "When it matters, challenge or build on the points others make.",
                    ),
                )
                session.add(role)
                await session.flush()
                role_ids.append(role.id)
                if r["title"] == "GC":
                    gc_agent_id = role.id
            logger.info("seed_progress", detail="Roles seeded.")
        else:
            logger.info("seed_progress", detail="Roles already exist, skipping.")
            # We need role_ids and gc_agent_id for subsequent sections
            role_ids = list((await session.execute(select(RoleAgent.id))).scalars().all())
            gc_stmt = select(RoleAgent.id).where(RoleAgent.title == "GC")
            gc_agent_id = (await session.execute(gc_stmt)).scalar()

        # 3. Sample Documents (skip if any documents exist)
        stmt = select(func.count()).select_from(Document)
        res = await session.execute(stmt)
        if res.scalar() == 0:
            docs_to_create: list[dict[str, Any]] = [
                {
                    "name": "Quality Incident Report #842",
                    "scope": "company",
                    "owner": None,
                    "text": (
                        "Incident #842: A major manufacturing defect was identified in Facility Alpha "
                        "on March 15. The primary defect involves improper sealing of the widget "
                        "casing, leading to a 4.2% failure rate in field stress tests. The root "
                        "cause appears to be a miscalibrated thermal press on Assembly Line 3."
                    ),
                },
                {
                    "name": "Confidential Legal Assessment",
                    "scope": "agent",
                    "owner": gc_agent_id,
                    "text": (
                        "Privileged and Confidential: Based on the defect rate reported in Incident "
                        "#842, our exposure to liability claims could exceed $5M if a proactive "
                        "recall is not initiated. Consumer protection regulations require "
                        "disclosure within 72 hours of identifying a systemic defect of this nature."
                    ),
                },
            ]

            default_doc_ids: list[str] = []
            for d in docs_to_create:
                try:
                    doc = Document(
                        document_name=d["name"],
                        library_scope=d["scope"],
                        owner_agent_id=d["owner"],
                        file_type="text/plain",
                        metadata_json={"type": "seed"},
                    )
                    session.add(doc)
                    await session.flush()

                    embeddings = await generate_embeddings([d["text"]], session)
                    chunk = DocumentChunk(
                        document_id=doc.id,
                        chunk_index=0,
                        page_number="1",
                        text=d["text"],
                        normalized_text=d["text"].lower(),
                        embedding=embeddings[0],
                    )
                    session.add(chunk)
                    default_doc_ids.append(str(doc.id))
                except Exception as e:
                    logger.info(
                        "seed_progress", detail=f"Failed to seed document '{d['name']}': {str(e)}"
                    )
                    # Continue with other documents if possible
                    continue
            logger.info("seed_progress", detail="Sample documents seeding process complete.")
        else:
            logger.info("seed_progress", detail="Documents already exist, skipping.")
            existing_ids = (await session.execute(select(Document.id))).scalars().all()
            default_doc_ids = [str(uid) for uid in existing_ids]

        # 4. Templates
        templates_data: list[dict[str, Any]] = [
            {
                "name": "Crisis Incident Response",
                "description": "High-stakes executive alignment for urgent operational failures.",
                "is_builtin": True,
                "brief": (
                    "A systemic manufacturing defect in the 'Aurora Regulator Pro' has been identified at Facility Alpha. "
                    "Initial field reports indicate a 4% failure rate, which poses significant brand and financial risk. "
                    "We need to decide on the scale of containment and how to manage stakeholder expectations."
                ),
                "objective": "Align on immediate containment (shipping hold vs. recall) and approve the external communication strategy.",
                "agenda": (
                    "1. Detailed Incident Report\n"
                    "2. Financial Disclosure Risk\n"
                    "3. Quality & Safety Assessment\n"
                    "4. Regulatory/Legal Obligations\n"
                    "5. Executive Decision on Containment"
                ),
                "expectations": "A clear decision on whether to initiate a full recall by the end of this session.",
                "default_selected_attendee_ids": [
                    str(uid) for uid in role_ids[:4]
                ],  # CEO, FD, GC, Mng
                "default_document_ids": default_doc_ids,
            },
            {
                "name": "Strategic Product Roadmap",
                "description": "Cross-functional planning for upcoming major releases.",
                "is_builtin": True,
                "brief": (
                    "The company is planning its next major product release. We need to align on the core features, "
                    "target market, and technical feasibility to ensure a successful launch that meets both customer "
                    "needs and technical standards."
                ),
                "objective": "Finalise the top 3 priorities for the upcoming release and identify any critical resource gaps.",
                "agenda": (
                    "1. Strategic Goals & Vision\n"
                    "2. Core Feature Proposals\n"
                    "3. Technical Architecture & Scalability\n"
                    "4. Market Position & Sales Readiness\n"
                    "5. Resource Allocation & Timeline"
                ),
                "expectations": "Agreement on the MVP feature set and committed timelines from Engineering and Sales.",
                "default_selected_attendee_ids": [
                    str(role_ids[0]),
                    str(role_ids[9]),
                    str(role_ids[12]),
                    str(role_ids[13]),
                ],  # CEO, Chief Architect, VP Sales, CSM
                "default_document_ids": [],
            },
            {
                "name": "Cybersecurity Breach Response",
                "description": "Rapid response coordination for security incidents.",
                "is_builtin": True,
                "brief": (
                    "A potential data breach has been detected in the customer portal. Preliminary analysis suggests "
                    "unauthorized access to encrypted user records. We must determine the scope and initiate "
                    "standard response protocols immediately."
                ),
                "objective": "Confirm the extent of the breach, initiate legal disclosure protocols, and launch immediate remediation steps.",
                "agenda": (
                    "1. Incident Status & Current Impact\n"
                    "2. Technical Forensic Findings\n"
                    "3. Legal, Privacy & Compliance Obligations\n"
                    "4. Stakeholder & Public Communication Strategy\n"
                    "5. Remediation & Long-term Strengthening"
                ),
                "expectations": "Approval of the initial incident disclosure statement and a locked-down containment plan.",
                "default_selected_attendee_ids": [
                    str(role_ids[0]),
                    str(role_ids[2]),
                    str(role_ids[9]),
                    str(role_ids[11]),
                ],  # CEO, GC, Chief Architect, SysAdmin
                "default_document_ids": [],
            },
        ]

        for t_data in templates_data:
            tmpl_stmt = select(MeetingTemplate).where(MeetingTemplate.name == t_data["name"])
            existing = (await session.execute(tmpl_stmt)).scalar()
            if existing:
                for key, value in t_data.items():
                    setattr(existing, key, value)
                logger.info("seed_progress", detail=f"Updated built-in template: {t_data['name']}")
            else:
                template = MeetingTemplate(**t_data)
                session.add(template)
                logger.info(
                    "seed_progress", detail=f"Seeded new built-in template: {t_data['name']}"
                )

        await session.commit()


if __name__ == "__main__":
    # Retained for manual/CI use. The API no longer shells out to this module:
    # system.py used subprocess.run(check=True) inside an async BackgroundTask,
    # which blocked the event loop for the whole seed (network embedding calls
    # included) and spawned a second process with its own DB engine.
    asyncio.run(seed_data())
