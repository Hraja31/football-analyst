-- Remove the m_smoke_test match created while verifying the Modal wiring.
-- Run in the Supabase SQL editor.
--
-- Order matters: player_match_stats, reports and team_reports all carry an FK
-- to matches(id) with no ON DELETE CASCADE, so the children go first.

begin;

delete from reports            where match_id = 'm_smoke_test';
delete from player_match_stats where match_id = 'm_smoke_test';
delete from team_reports       where match_id = 'm_smoke_test';
delete from matches            where id       = 'm_smoke_test';

commit;
