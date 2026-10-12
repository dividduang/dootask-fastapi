-- Run once, on the empty new-install schema; never on an existing deployment.
-- Only settings defaults and the empty-history completion marker; no source demo accounts.
SET NAMES utf8mb4 COLLATE utf8mb4_unicode_ci;
START TRANSACTION;
SET @schema_seed_at = CURRENT_TIMESTAMP;

INSERT INTO `settings` (`name`, `desc`, `setting`, `created_at`, `updated_at`) VALUES
  ('system', '', '{"reg":"open","project_invite":"open","login_code":"auto"}', @schema_seed_at, @schema_seed_at),
  ('priority', '', '[{"name":"重要且紧急","color":"#ED4014","days":1,"priority":1,"is_default":1},{"name":"重要不紧急","color":"#F16B62","days":3,"priority":2,"is_default":0},{"name":"紧急不重要","color":"#19C919","days":5,"priority":3,"is_default":0},{"name":"不重要不紧急","color":"#2D8CF0","days":0,"priority":4,"is_default":0}]', @schema_seed_at, @schema_seed_at),
  ('columnTemplate', '', '[{"name":"软件开发","columns":["产品规划","前端开发","后端开发","测试","发布","其他"]},{"name":"产品开发","columns":["产品计划","正在设计","正在研发","测试","准备发布","发布成功"]}]', @schema_seed_at, @schema_seed_at),
  ('checkinSetting', '', '{"modes":["auto"]}', @schema_seed_at, @schema_seed_at),
  ('emailSetting', '', '{"msg_unread_time_ranges":[["00:00","09:00"],["18:00","23:59"]]}', @schema_seed_at, @schema_seed_at);

INSERT INTO `web_socket_dialog_msg_attachment_backfills`
  (`status`, `before_msg_id`, `last_msg_id`, `completed_at`, `created_at`, `updated_at`)
VALUES
  ('completed', 0, 0, @schema_seed_at, @schema_seed_at, @schema_seed_at);

COMMIT;
