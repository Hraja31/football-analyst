-- Footage → Tactics — per-club schema (BUILD_SPEC §8)
-- Run in the Supabase SQL editor. Safe to re-run.

create table if not exists clubs (
  id text primary key,
  name text,
  veo_account text
);

create table if not exists players (
  id text primary key,
  club_id text references clubs(id),
  name text,
  age int,
  height_cm int,
  photo_url text,
  jersey_number int,
  position text,
  strong_foot text
);

create table if not exists matches (
  id text primary key,
  club_id text references clubs(id),
  date date,
  opponent text,
  footage_url text
);

create table if not exists player_match_stats (
  player_id text references players(id),
  match_id text references matches(id),
  -- CV-derived (null when the CV branch degrades — see workflow node 4b)
  heatmap_url text,
  distance_m real,
  top_speed_kmh real,
  avg_x real,
  avg_y real,
  zone_share jsonb,
  id_confidence real,
  -- manual, entered by the coach on the form
  goals int, assists int, shots int, shots_on_target int,
  tackles int, tackles_won int, fouls int, yellow int, red int,
  saves int, shots_faced int,
  primary key (player_id, match_id)
);

create table if not exists reports (
  player_id text references players(id),
  match_id text references matches(id),
  rating real,
  swot jsonb,
  summary text,
  training_plan jsonb,
  primary key (player_id, match_id)
);

create table if not exists team_reports (
  match_id text primary key references matches(id),
  team_swot jsonb,
  formation text,
  best_xi jsonb,
  position_weaknesses jsonb,
  playing_style_note text
);

-- The frontend reads reports by match; the pipeline writes by (player, match).
create index if not exists idx_pms_match on player_match_stats(match_id);
create index if not exists idx_reports_match on reports(match_id);

-- Public bucket for heatmap images (or create it in the Storage UI named 'heatmaps').
insert into storage.buckets (id, name, public)
values ('heatmaps', 'heatmaps', true)
on conflict (id) do nothing;

-- ── What the report endpoint reads ───────────────────────────────────────────
-- The pipeline writes by (player, match) across three tables; the frontend wants
-- one row per match. This view does that fold, so workflow node 12b needs a
-- single GET (/rest/v1/v_match_report?match_id=eq.…) instead of three.
create or replace view v_match_report as
select
  tr.match_id,
  jsonb_build_object(
    'formation',           tr.formation,
    'best_xi',             tr.best_xi,
    'team_swot',           tr.team_swot,
    'position_weaknesses', tr.position_weaknesses,
    'playing_style_note',  tr.playing_style_note
  ) as team,
  coalesce((
    select jsonb_agg(
             jsonb_build_object(
               'player_id',     r.player_id,
               'rating',        r.rating,
               'swot',          r.swot,
               'summary',       r.summary,
               'training_plan', r.training_plan)
             order by r.rating desc nulls last)
    from reports r
    where r.match_id = tr.match_id), '[]'::jsonb) as players,
  -- Null CV columns are the degraded run (node 4b). The endpoint reads an empty
  -- cv_players as "no spatial data", which is exactly what it means.
  coalesce((
    select jsonb_agg(
             jsonb_build_object(
               'player_id',     s.player_id,
               'heatmap_url',   s.heatmap_url,
               'id_confidence', s.id_confidence,
               'distance_m',    s.distance_m,
               'top_speed_kmh', s.top_speed_kmh,
               'avg_position',  case when s.avg_x is null then null
                                     else jsonb_build_object('x', s.avg_x, 'y', s.avg_y) end,
               'zone_share',    s.zone_share))
    from player_match_stats s
    where s.match_id = tr.match_id
      and s.heatmap_url is not null), '[]'::jsonb) as cv_players
from team_reports tr;
