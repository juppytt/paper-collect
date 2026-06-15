create table if not exists papers (
    id integer primary key,
    dblp_key text not null unique,
    venue text not null,
    year integer not null,
    title text not null,
    authors_json text not null,
    booktitle text,
    pages text,
    crossref text not null,
    dblp_url text,
    doi text,
    ee_json text not null,
    source text not null,
    abstract text,
    pdf_url text,
    pdf_path text,
    text_path text,
    created_at text not null default current_timestamp,
    updated_at text not null default current_timestamp
);

create index if not exists idx_papers_venue_year on papers (venue, year);
create index if not exists idx_papers_crossref on papers (crossref);
create index if not exists idx_papers_doi on papers (doi);

create table if not exists crawl_state (
    downloader text not null,
    scope text not null,
    status text not null,
    payload_json text,
    updated_at text not null default current_timestamp,
    primary key (downloader, scope)
);
