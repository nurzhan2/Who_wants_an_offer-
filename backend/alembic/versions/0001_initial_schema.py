"""Initial schema: profiles, vacancies, matches, applications, pipeline runs.

Hand-written rather than left as autogenerate produced it, for three reasons:

* Alembic autogenerate neither creates nor drops PostgreSQL enum types. Left
  alone it emits ``postgresql.ENUM(...)`` without ``create_type=False``, which
  auto-creates the type on first use and then fails on the second table that
  uses the same type — ``seniority`` and ``remote_type`` are each used twice.
  The failure only shows on a clean database, i.e. at the first real deploy.
  Types are therefore created explicitly below and dropped in downgrade.
* ``CREATE EXTENSION vector`` has to happen before any vector column exists.
* HNSW, GIN and partial indexes cannot be expressed by autogenerate. They are
  written as raw SQL and named ``ix_pg_*``; env.py excludes that prefix so a
  later autogenerate does not try to drop them.

Enum member lists are frozen copies, not imports from app.db.enums: a migration
must keep applying the same way after the application's enums change.

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Vector width is a literal here on purpose. settings.EMBEDDING_DIM may change;
#: an already-applied migration may not. A mismatch between the two is caught at
#: application start by app.db.checks.verify_embedding_dimension.
EMBEDDING_DIM = 1024

#: name -> members, in creation order.
ENUM_TYPES: dict[str, tuple[str, ...]] = {
    "skill_level": ("basic", "working", "strong", "expert"),
    "seniority": ("junior", "middle", "senior", "lead"),
    "remote_type": ("no", "hybrid", "full"),
    "employment_type": ("full_time", "part_time", "contract", "internship", "freelance"),
    "salary_period": ("hour", "day", "month", "year"),
    "match_bucket": ("apply_now", "strong", "stretch", "skip", "filtered"),
    "application_status": (
        "saved",
        "applied",
        "screening",
        "interview",
        "offer",
        "rejected",
    ),
    "pipeline_run_status": ("running", "success", "partial", "failed"),
}


def _enum(name: str) -> postgresql.ENUM:
    """Reference an already-created type; never create one implicitly."""
    return postgresql.ENUM(*ENUM_TYPES[name], name=name, create_type=False)


def upgrade() -> None:
    """Create the extension, the enum types, the tables and the indexes."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    for name, members in ENUM_TYPES.items():
        values = ", ".join(f"'{member}'" for member in members)
        op.execute(f"CREATE TYPE {name} AS ENUM ({values})")

    op.create_table(
        "candidate_profile",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column("headline", sa.String(length=300), nullable=True),
        sa.Column("seniority", _enum("seniority"), nullable=True),
        sa.Column("total_years", sa.Numeric(precision=4, scale=1), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("locations", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("relocation", sa.Boolean(), nullable=False),
        sa.Column("remote_pref", _enum("remote_type"), nullable=True),
        sa.Column("salary_min", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("salary_currency", sa.CHAR(length=3), nullable=True),
        sa.Column("languages", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candidate_profile")),
    )

    op.create_table(
        "profile_skill",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("profile_id", sa.UUID(), nullable=False),
        sa.Column("canonical_name", sa.String(length=100), nullable=False),
        sa.Column("raw_name", sa.String(length=200), nullable=True),
        sa.Column("years", sa.Numeric(precision=4, scale=1), nullable=True),
        sa.Column("level", _enum("skill_level"), nullable=False),
        sa.Column("last_used_year", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["candidate_profile.id"],
            name=op.f("fk_profile_skill_profile_id_candidate_profile"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_profile_skill")),
        sa.UniqueConstraint(
            "profile_id",
            "canonical_name",
            name=op.f("uq_profile_skill_profile_id_canonical_name"),
        ),
    )
    op.create_index(
        op.f("ix_profile_skill_profile_id"), "profile_skill", ["profile_id"], unique=False
    )

    op.create_table(
        "vacancy",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("fingerprint", sa.String(length=40), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("company", sa.String(length=200), nullable=True),
        sa.Column("company_url", sa.String(length=500), nullable=True),
        sa.Column("description_raw", sa.Text(), nullable=True),
        sa.Column("description_md", sa.Text(), nullable=True),
        sa.Column("seniority", _enum("seniority"), nullable=True),
        sa.Column("min_years", sa.Numeric(precision=4, scale=1), nullable=True),
        sa.Column("city", sa.String(length=120), nullable=True),
        sa.Column("country", sa.CHAR(length=2), nullable=True),
        sa.Column("remote", _enum("remote_type"), nullable=False),
        sa.Column("salary_min", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("salary_max", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("currency", sa.CHAR(length=3), nullable=True),
        sa.Column("is_gross", sa.Boolean(), nullable=True),
        sa.Column("period", _enum("salary_period"), nullable=True),
        sa.Column("salary_min_normalized", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("salary_max_normalized", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("salary_normalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("employment_type", _enum("employment_type"), nullable=True),
        sa.Column("language", sa.CHAR(length=2), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('simple', coalesce(title, '') || ' ' || "
                "coalesce(company, '') || ' ' || coalesce(description_raw, ''))",
                persisted=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vacancy")),
        sa.UniqueConstraint("fingerprint", name=op.f("uq_vacancy_fingerprint")),
    )

    op.create_table(
        "vacancy_source",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("vacancy_id", sa.UUID(), nullable=False),
        sa.Column("source_slug", sa.String(length=50), nullable=False),
        sa.Column("external_id", sa.String(length=200), nullable=False),
        sa.Column("url", sa.String(length=1000), nullable=False),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["vacancy_id"],
            ["vacancy.id"],
            name=op.f("fk_vacancy_source_vacancy_id_vacancy"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vacancy_source")),
        sa.UniqueConstraint(
            "source_slug", "external_id", name=op.f("uq_vacancy_source_source_slug_external_id")
        ),
    )
    op.create_index(
        op.f("ix_vacancy_source_vacancy_id"), "vacancy_source", ["vacancy_id"], unique=False
    )

    op.create_table(
        "vacancy_skill",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("vacancy_id", sa.UUID(), nullable=False),
        sa.Column("canonical_name", sa.String(length=100), nullable=False),
        sa.Column("is_required", sa.Boolean(), nullable=False),
        sa.Column("weight", sa.Numeric(precision=3, scale=2), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["vacancy_id"],
            ["vacancy.id"],
            name=op.f("fk_vacancy_skill_vacancy_id_vacancy"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vacancy_skill")),
        sa.UniqueConstraint(
            "vacancy_id", "canonical_name", name=op.f("uq_vacancy_skill_vacancy_id_canonical_name")
        ),
    )
    op.create_index(
        op.f("ix_vacancy_skill_vacancy_id"), "vacancy_skill", ["vacancy_id"], unique=False
    )

    op.create_table(
        "match",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("profile_id", sa.UUID(), nullable=False),
        sa.Column("vacancy_id", sa.UUID(), nullable=False),
        sa.Column("score", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column("rule_score", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column("semantic_score", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("llm_score", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("bucket", _enum("match_bucket"), nullable=False),
        sa.Column("component_scores", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("matched_skills", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("missing_required", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("missing_nice", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("red_flags", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("experience_gap_years", sa.Numeric(precision=4, scale=1), nullable=True),
        sa.Column("verdict", sa.Text(), nullable=True),
        sa.Column("application_angle", sa.Text(), nullable=True),
        sa.Column(
            "scored_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["candidate_profile.id"],
            name=op.f("fk_match_profile_id_candidate_profile"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["vacancy_id"],
            ["vacancy.id"],
            name=op.f("fk_match_vacancy_id_vacancy"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_match")),
        sa.UniqueConstraint(
            "profile_id", "vacancy_id", name=op.f("uq_match_profile_id_vacancy_id")
        ),
    )
    op.create_index(op.f("ix_match_vacancy_id"), "match", ["vacancy_id"], unique=False)

    op.create_table(
        "application",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("vacancy_id", sa.UUID(), nullable=False),
        sa.Column("status", _enum("application_status"), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("cover_letter", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["vacancy_id"],
            ["vacancy.id"],
            name=op.f("fk_application_vacancy_id_vacancy"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_application")),
    )
    op.create_index(op.f("ix_application_vacancy_id"), "application", ["vacancy_id"], unique=False)

    op.create_table(
        "pipeline_run",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("source_slug", sa.String(length=50), nullable=False),
        sa.Column("status", _enum("pipeline_run_status"), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("found", sa.Integer(), nullable=False),
        sa.Column("new", sa.Integer(), nullable=False),
        sa.Column("updated", sa.Integer(), nullable=False),
        sa.Column("errors", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pipeline_run")),
    )
    op.create_index(
        op.f("ix_pipeline_run_source_slug"), "pipeline_run", ["source_slug"], unique=False
    )

    # ── PostgreSQL-specific indexes (ix_pg_*, hand-managed) ───────────
    # Approximate nearest neighbour over the embeddings. m / ef_construction
    # trade index build time for recall; 16 / 64 is pgvector's documented
    # starting point and is revisited when there is real data to measure.
    op.execute(
        "CREATE INDEX ix_pg_vacancy_embedding_hnsw ON vacancy "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )
    op.execute(
        "CREATE INDEX ix_pg_candidate_profile_embedding_hnsw ON candidate_profile "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )
    op.execute(
        "CREATE INDEX ix_pg_vacancy_search_vector_gin ON vacancy USING gin (search_vector)"
    )
    # Partial: the dashboard never lists inactive vacancies, so they do not
    # belong in the index the list query walks.
    op.execute(
        "CREATE INDEX ix_pg_vacancy_published_at_active ON vacancy "
        "(published_at DESC NULLS LAST, id DESC) WHERE is_active"
    )
    # Sorting by pay uses the normalised column, never the advertised one.
    op.execute(
        "CREATE INDEX ix_pg_vacancy_salary_normalized_active ON vacancy "
        "(salary_min_normalized DESC NULLS LAST, id DESC) WHERE is_active"
    )
    # Serves the keyset scan of "best matches for this profile".
    op.execute(
        "CREATE INDEX ix_pg_match_profile_score ON match (profile_id, score DESC, vacancy_id DESC)"
    )


def downgrade() -> None:
    """Drop everything upgrade created, in reverse dependency order."""
    for index_name in (
        "ix_pg_match_profile_score",
        "ix_pg_vacancy_salary_normalized_active",
        "ix_pg_vacancy_published_at_active",
        "ix_pg_vacancy_search_vector_gin",
        "ix_pg_candidate_profile_embedding_hnsw",
        "ix_pg_vacancy_embedding_hnsw",
    ):
        op.execute(f"DROP INDEX IF EXISTS {index_name}")

    op.drop_index(op.f("ix_pipeline_run_source_slug"), table_name="pipeline_run")
    op.drop_table("pipeline_run")
    op.drop_index(op.f("ix_application_vacancy_id"), table_name="application")
    op.drop_table("application")
    op.drop_index(op.f("ix_match_vacancy_id"), table_name="match")
    op.drop_table("match")
    op.drop_index(op.f("ix_vacancy_skill_vacancy_id"), table_name="vacancy_skill")
    op.drop_table("vacancy_skill")
    op.drop_index(op.f("ix_vacancy_source_vacancy_id"), table_name="vacancy_source")
    op.drop_table("vacancy_source")
    op.drop_table("vacancy")
    op.drop_index(op.f("ix_profile_skill_profile_id"), table_name="profile_skill")
    op.drop_table("profile_skill")
    op.drop_table("candidate_profile")

    # Types go after the tables that use them; autogenerate would never do this.
    for name in reversed(list(ENUM_TYPES)):
        op.execute(f"DROP TYPE IF EXISTS {name}")

    op.execute("DROP EXTENSION IF EXISTS vector")
