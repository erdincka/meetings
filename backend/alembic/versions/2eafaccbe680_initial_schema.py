"""initial schema

Revision ID: 2eafaccbe680
Revises: 
Create Date: 2026-08-22 22:27:56.349764
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import pgvector.sqlalchemy


revision: str = '2eafaccbe680'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # pgvector is mounted into the Postgres pod as a declarative CNPG
    # ImageVolume extension (deploy/bootstrap/cnpg-cluster.yaml), but the
    # extension still has to be created inside the database.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table('meeting_templates',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('brief', sa.Text(), nullable=True),
    sa.Column('objective', sa.Text(), nullable=True),
    sa.Column('expectations', sa.Text(), nullable=True),
    sa.Column('agenda', sa.Text(), nullable=True),
    sa.Column('default_selected_attendee_ids', sa.JSON(), nullable=False),
    sa.Column('default_document_ids', sa.JSON(), nullable=False),
    sa.Column('is_builtin', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    schema='meetings'
    )
    op.create_table('role_agents',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('display_name', sa.String(length=100), nullable=False),
    sa.Column('title', sa.String(length=100), nullable=False),
    sa.Column('department', sa.String(length=100), nullable=False),
    sa.Column('seniority', sa.String(length=50), nullable=True),
    sa.Column('summary', sa.Text(), nullable=True),
    sa.Column('responsibilities', sa.JSON(), nullable=False),
    sa.Column('kpis', sa.JSON(), nullable=False),
    sa.Column('priorities', sa.JSON(), nullable=False),
    sa.Column('objectives', sa.JSON(), nullable=False),
    sa.Column('risk_tolerance', sa.String(length=50), nullable=True),
    sa.Column('tone', sa.JSON(), nullable=False),
    sa.Column('collaboration_style', sa.String(length=100), nullable=True),
    sa.Column('challenge_style', sa.String(length=100), nullable=True),
    sa.Column('allowed_shared_library_access', sa.Boolean(), nullable=False),
    sa.Column('private_library_id', sa.UUID(), nullable=False),
    sa.Column('system_prompt', sa.Text(), nullable=True),
    sa.Column('default_tools', sa.JSON(), nullable=False),
    sa.Column('ui_metadata', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('private_library_id'),
    schema='meetings'
    )
    op.create_table('system_settings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('debug', sa.Boolean(), nullable=False),
    sa.Column('retrieval_limits_per_agent', sa.Integer(), nullable=False),
    sa.Column('max_evidence_per_message', sa.Integer(), nullable=False),
    sa.Column('default_turn_limit', sa.Integer(), nullable=False),
    sa.Column('cleanup_rules', sa.String(length=64), nullable=False),
    sa.Column('inference_temperature', sa.Float(), nullable=False),
    sa.Column('supervisor_temperature', sa.Float(), nullable=False),
    sa.Column('supervisor_prompt', sa.Text(), nullable=True),
    sa.Column('agent_prompt', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    schema='meetings'
    )
    op.create_table('meetings',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('status', sa.String(length=50), nullable=False),
    sa.Column('brief', sa.Text(), nullable=True),
    sa.Column('agenda', sa.Text(), nullable=True),
    sa.Column('objective', sa.Text(), nullable=True),
    sa.Column('expectations', sa.Text(), nullable=True),
    sa.Column('selected_attendee_ids', sa.JSON(), nullable=False),
    sa.Column('turn_limit', sa.Integer(), nullable=False),
    sa.Column('current_turn', sa.Integer(), nullable=False),
    sa.Column('template_id', sa.UUID(), nullable=True),
    sa.Column('meeting_log', sa.JSON(), nullable=False),
    sa.Column('citations', sa.JSON(), nullable=False),
    sa.Column('warnings', sa.JSON(), nullable=False),
    sa.Column('final_summary', sa.Text(), nullable=True),
    sa.Column('active_agent_id', sa.String(length=50), nullable=True),
    sa.Column('stop_requested', sa.Boolean(), nullable=False),
    sa.Column('terminated', sa.Boolean(), nullable=False),
    sa.Column('uploaded_brief_docs', sa.JSON(), nullable=False),
    sa.Column('settings_snapshot', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['template_id'], ['meetings.meeting_templates.id'], ),
    sa.PrimaryKeyConstraint('id'),
    schema='meetings'
    )
    op.create_table('documents',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('document_name', sa.String(length=255), nullable=False),
    sa.Column('library_scope', sa.String(length=50), nullable=False),
    sa.Column('owner_agent_id', sa.UUID(), nullable=True),
    sa.Column('meeting_id', sa.UUID(), nullable=True),
    sa.Column('file_type', sa.String(length=50), nullable=True),
    sa.Column('metadata_json', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['meeting_id'], ['meetings.meetings.id'], ),
    sa.ForeignKeyConstraint(['owner_agent_id'], ['meetings.role_agents.id'], ),
    sa.PrimaryKeyConstraint('id'),
    schema='meetings'
    )
    op.create_index(op.f('ix_meetings_documents_library_scope'), 'documents', ['library_scope'], unique=False, schema='meetings')
    op.create_index(op.f('ix_meetings_documents_meeting_id'), 'documents', ['meeting_id'], unique=False, schema='meetings')
    op.create_index(op.f('ix_meetings_documents_owner_agent_id'), 'documents', ['owner_agent_id'], unique=False, schema='meetings')
    op.create_table('document_chunks',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('document_id', sa.UUID(), nullable=False),
    sa.Column('chunk_index', sa.Integer(), nullable=False),
    sa.Column('page_number', sa.String(length=50), nullable=True),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('normalized_text', sa.Text(), nullable=True),
    sa.Column('embedding', pgvector.sqlalchemy.vector.VECTOR(dim=2048), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['document_id'], ['meetings.documents.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    schema='meetings'
    )
    op.create_index(op.f('ix_meetings_document_chunks_document_id'), 'document_chunks', ['document_id'], unique=False, schema='meetings')
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index(op.f('ix_meetings_document_chunks_document_id'), table_name='document_chunks', schema='meetings')
    op.drop_table('document_chunks', schema='meetings')
    op.drop_index(op.f('ix_meetings_documents_owner_agent_id'), table_name='documents', schema='meetings')
    op.drop_index(op.f('ix_meetings_documents_meeting_id'), table_name='documents', schema='meetings')
    op.drop_index(op.f('ix_meetings_documents_library_scope'), table_name='documents', schema='meetings')
    op.drop_table('documents', schema='meetings')
    op.drop_table('meetings', schema='meetings')
    op.drop_table('system_settings', schema='meetings')
    op.drop_table('role_agents', schema='meetings')
    op.drop_table('meeting_templates', schema='meetings')
    # ### end Alembic commands ###
