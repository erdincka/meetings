import asyncio
import random

import structlog
from sqlalchemy import func, select

from app.core.database import require_session_maker
from app.models.documents import Document, DocumentChunk
from app.models.meetings import MeetingTemplate
from app.models.roles import RoleAgent
from app.services.embedding_service import generate_embeddings

logger = structlog.get_logger(__name__)

# Persona traits were drawn from an unseeded RNG, so every install produced
# agents that behaved differently and demos were not reproducible. A fixed
# seed keeps the variety while making it deterministic.
SEED_RNG = random.Random(20260401)


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
            roles_data = [
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

            role_ids = []
            gc_agent_id = None
            for r in roles_data:
                role = RoleAgent(
                    display_name=r["name"],
                    title=r["title"],
                    department=r["dept"],
                    seniority=r.get("seniority", "Senior"),
                    summary=f"The {r['name']} responsible for {r['dept']} strategy and execution.",
                    priorities=[f"Optimize {r['dept']}", "Reduce risk", "Drive value"],
                    risk_tolerance="Low"
                    if "Legal" in r["dept"] or "Quality" in r["dept"]
                    else "Medium",
                    tone=SEED_RNG.sample(tones_pool, SEED_RNG.randint(2, 3)),
                    collaboration_style=SEED_RNG.choice(collab_pool),
                    challenge_style=SEED_RNG.choice(challenge_pool),
                    system_prompt=(
                        "Always represent your department's interests strongly. "
                        "You have access to what other attendees have said. When it matters, "
                        "challenge or build on their points from your department's perspective."
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
            role_ids = (await session.execute(select(RoleAgent.id))).scalars().all()
            gc_stmt = select(RoleAgent.id).where(RoleAgent.title == "GC")
            gc_agent_id = (await session.execute(gc_stmt)).scalar()

        # 3. Sample Documents (skip if any documents exist)
        stmt = select(func.count()).select_from(Document)
        res = await session.execute(stmt)
        if res.scalar() == 0:
            docs_to_create = [
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

            default_doc_ids = []
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
            default_doc_ids = (await session.execute(select(Document.id))).scalars().all()
            default_doc_ids = [str(uid) for uid in default_doc_ids]

        # 4. Templates
        templates_data = [
            {
                "name": "Crisis Incident Response",
                "description": "High-stakes executive alignment for urgent operational failures.",
                "is_builtin": True,
                "brief": (
                    "A systemic manufacturing defect in the 'Nexus Widget' has been identified at Facility Alpha. "
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
            stmt = select(MeetingTemplate).where(MeetingTemplate.name == t_data["name"])
            existing = (await session.execute(stmt)).scalar()
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
