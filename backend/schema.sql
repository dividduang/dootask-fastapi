-- New empty MariaDB 10.7 database only. No users, legacy data or app-install state.
-- Source mapping: docs/evidence/implementation/schema-migrations.json and schema-models.json.
-- Apply schema_seed.sql once after this file. Security extensions are separately owned.
SET NAMES utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE `file_contents` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `fid` BIGINT NULL DEFAULT 0 COMMENT '文件ID',
  `content` LONGTEXT NULL DEFAULT NULL COMMENT '内容',
  `text` TEXT NULL DEFAULT NULL COMMENT '内容（主要用于文档类型搜索）',
  `size` BIGINT NULL DEFAULT 0 COMMENT '大小(B)',
  `userid` BIGINT NULL DEFAULT 0 COMMENT '会员ID',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  `deleted_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `file_contents_fid_index` (`fid`),
  KEY `file_contents_userid_index` (`userid`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `files` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `pid` BIGINT NULL DEFAULT 0 COMMENT '上级ID',
  `pids` VARCHAR(255) NULL DEFAULT '' COMMENT '上级ID递归',
  `cid` BIGINT NULL DEFAULT 0 COMMENT '复制ID',
  `name` VARCHAR(255) NULL DEFAULT '' COMMENT '名称',
  `type` VARCHAR(20) NULL DEFAULT '' COMMENT '类型',
  `ext` VARCHAR(20) NULL DEFAULT '' COMMENT '后缀名',
  `size` BIGINT NULL DEFAULT 0 COMMENT '大小(B)',
  `hash` CHAR(32) NULL DEFAULT NULL COMMENT '文件内容 md5（分片上传秒传用）',
  `userid` BIGINT NULL DEFAULT 0 COMMENT '拥有者ID',
  `share` INT NULL DEFAULT 0 COMMENT '是否共享',
  `guest_access` TINYINT NULL DEFAULT 0 COMMENT '是否允许游客访问',
  `pshare` BIGINT NULL DEFAULT 0 COMMENT '所属分享ID',
  `created_id` BIGINT NULL DEFAULT 0 COMMENT '创建者',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  `deleted_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `files_pid_index` (`pid`),
  KEY `files_cid_index` (`cid`),
  KEY `files_hash_index` (`hash`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `project_columns` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `project_id` BIGINT NULL DEFAULT 0 COMMENT '项目ID',
  `name` VARCHAR(100) NULL DEFAULT '' COMMENT '列表名称',
  `color` VARCHAR(20) NULL DEFAULT '' COMMENT '颜色',
  `sort` INT NULL DEFAULT 0 COMMENT '排序(ASC)',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  `deleted_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `project_logs` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `project_id` BIGINT NULL DEFAULT 0 COMMENT '项目ID',
  `column_id` BIGINT NULL DEFAULT 0 COMMENT '列表ID',
  `task_id` BIGINT NULL DEFAULT 0 COMMENT '任务ID',
  `task_only` INT NULL DEFAULT 0 COMMENT '仅任务日志：0否，1是',
  `userid` BIGINT NULL DEFAULT 0 COMMENT '会员ID',
  `detail` VARCHAR(500) NULL DEFAULT '' COMMENT '详细信息',
  `record` TEXT NULL DEFAULT NULL COMMENT '记录数据',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `project_logs_project_id_index` (`project_id`),
  KEY `project_logs_task_id_index` (`task_id`),
  KEY `project_logs_task_only_index` (`task_only`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `project_task_contents` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `project_id` BIGINT NULL DEFAULT 0 COMMENT '项目ID',
  `task_id` BIGINT NULL DEFAULT 0 COMMENT '任务ID',
  `userid` BIGINT NULL DEFAULT 0 COMMENT '用户ID',
  `desc` VARCHAR(500) NULL DEFAULT '' COMMENT '内容描述',
  `content` LONGTEXT NULL DEFAULT NULL COMMENT '内容',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `project_task_contents_project_id_index` (`project_id`),
  KEY `project_task_contents_task_id_index` (`task_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `project_task_files` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `project_id` BIGINT NULL DEFAULT 0 COMMENT '项目ID',
  `task_id` BIGINT NULL DEFAULT 0 COMMENT '任务ID',
  `name` VARCHAR(100) NULL DEFAULT '' COMMENT '文件名称',
  `size` BIGINT NULL DEFAULT 0 COMMENT '文件大小(B)',
  `ext` VARCHAR(20) NULL DEFAULT '' COMMENT '文件格式',
  `path` VARCHAR(255) NULL DEFAULT '' COMMENT '文件地址',
  `thumb` VARCHAR(255) NULL DEFAULT '' COMMENT '缩略图',
  `userid` BIGINT NULL DEFAULT 0 COMMENT '上传用户ID',
  `download` INT NULL DEFAULT 0 COMMENT '下载次数',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `project_task_files_project_id_index` (`project_id`),
  KEY `project_task_files_task_id_index` (`task_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `project_task_tags` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `project_id` BIGINT NULL DEFAULT 0 COMMENT '项目ID',
  `task_id` BIGINT NULL DEFAULT 0 COMMENT '任务ID',
  `name` VARCHAR(50) NULL DEFAULT '' COMMENT '标题',
  `color` VARCHAR(10) NULL DEFAULT '' COMMENT '颜色',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `project_task_tags_project_id_index` (`project_id`),
  KEY `project_task_tags_task_id_index` (`task_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `project_task_users` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `project_id` BIGINT NULL DEFAULT 0 COMMENT '项目ID',
  `task_id` BIGINT NULL DEFAULT 0 COMMENT '任务ID',
  `task_pid` BIGINT NULL DEFAULT 0 COMMENT '任务ID（如果是子任务则是父级任务ID）',
  `userid` BIGINT NULL DEFAULT 0 COMMENT '成员ID',
  `owner` INT NULL DEFAULT 0 COMMENT '是否任务负责人',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `project_task_users_userid_index` (`userid`),
  KEY `project_task_users_task_id_index` (`task_id`),
  KEY `project_task_users_task_pid_index` (`task_pid`),
  KEY `project_task_users_task_id_userid_index` (`task_id`, `userid`),
  KEY `project_task_users_owner_index` (`owner`),
  KEY `idx_ptu_task_owner_user` (`task_id`, `owner`, `userid`),
  KEY `idx_ptu_user_owner_task` (`userid`, `owner`, `task_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `project_tasks` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `parent_id` BIGINT NULL DEFAULT 0 COMMENT '父级任务ID',
  `project_id` BIGINT NULL DEFAULT 0 COMMENT '项目ID',
  `column_id` BIGINT NULL DEFAULT 0 COMMENT '列表ID',
  `dialog_id` BIGINT NULL DEFAULT 0 COMMENT '聊天会话ID',
  `flow_item_id` BIGINT NULL DEFAULT 0 COMMENT '工作流状态ID',
  `flow_item_name` VARCHAR(50) NULL DEFAULT '' COMMENT '工作流状态名称',
  `name` VARCHAR(255) NULL DEFAULT '' COMMENT '标题',
  `color` VARCHAR(20) NULL DEFAULT '' COMMENT '颜色',
  `desc` VARCHAR(500) NULL DEFAULT '' COMMENT '描述',
  `start_at` TIMESTAMP NULL DEFAULT NULL COMMENT '计划开始时间',
  `end_at` TIMESTAMP NULL DEFAULT NULL COMMENT '计划结束时间',
  `archived_at` TIMESTAMP NULL DEFAULT NULL COMMENT '归档时间',
  `archived_userid` BIGINT NULL DEFAULT 0 COMMENT '归档会员',
  `archived_follow` TINYINT NULL DEFAULT 0 COMMENT '跟随项目归档（项目取消归档时任务也取消归档）',
  `complete_at` TIMESTAMP NULL DEFAULT NULL COMMENT '完成时间',
  `userid` BIGINT NULL DEFAULT 0 COMMENT '创建人',
  `visibility` INT NULL DEFAULT 1 COMMENT '任务可见性：1-项目人员 2-任务人员 3-指定成员',
  `p_level` TINYINT NULL DEFAULT 0 COMMENT '优先级',
  `p_name` VARCHAR(50) NULL DEFAULT '' COMMENT '优先级名称',
  `p_color` VARCHAR(20) NULL DEFAULT '' COMMENT '优先级颜色',
  `sort` INT NULL DEFAULT 0 COMMENT '排序(ASC)',
  `loop` VARCHAR(20) NULL DEFAULT '' COMMENT '重复周期',
  `loop_at` TIMESTAMP NULL DEFAULT NULL COMMENT '下一次重复时间',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  `deleted_at` TIMESTAMP NULL DEFAULT NULL,
  `deleted_userid` BIGINT NULL DEFAULT 0 COMMENT '删除会员',
  PRIMARY KEY (`id`),
  KEY `project_tasks_parent_id_deleted_at_index` (`parent_id`, `deleted_at`),
  KEY `project_tasks_project_id_index` (`project_id`),
  KEY `project_tasks_visibility_index` (`visibility`),
  KEY `project_tasks_parent_id_index` (`parent_id`),
  KEY `project_tasks_dialog_id_index` (`dialog_id`),
  KEY `project_tasks_userid_index` (`userid`),
  KEY `project_tasks_project_id_archived_at_deleted_at_id_index` (`project_id`, `archived_at`, `deleted_at`, `id`),
  KEY `idx_pt_dashboard_due` (`project_id`, `parent_id`, `visibility`, `archived_at`, `deleted_at`, `complete_at`, `end_at`),
  KEY `idx_pt_dashboard_priority` (`project_id`, `parent_id`, `visibility`, `archived_at`, `deleted_at`, `complete_at`, `p_level`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `project_users` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `project_id` BIGINT NULL DEFAULT 0 COMMENT '项目ID',
  `userid` BIGINT NULL DEFAULT 0 COMMENT '成员ID',
  `owner` INT NULL DEFAULT 0 COMMENT '是否负责人',
  `top_at` TIMESTAMP NULL DEFAULT NULL COMMENT '置顶时间',
  `sort` INT NULL DEFAULT 0 COMMENT '排序(ASC)',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `project_users_userid_index` (`userid`),
  KEY `project_users_project_id_index` (`project_id`),
  KEY `project_users_project_id_userid_index` (`project_id`, `userid`),
  KEY `project_users_owner_index` (`owner`),
  KEY `project_users_userid_project_id_index` (`userid`, `project_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `projects` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `name` VARCHAR(255) NULL DEFAULT '' COMMENT '名称',
  `desc` VARCHAR(500) NULL DEFAULT '' COMMENT '描述、备注',
  `userid` BIGINT NULL DEFAULT 0 COMMENT '创建人',
  `personal` TINYINT(1) NULL DEFAULT 0 COMMENT '是否个人项目',
  `archive_method` VARCHAR(20) NULL DEFAULT 'system' COMMENT '自动归档方式',
  `archive_days` INT NULL DEFAULT 30 COMMENT '自动归档天数',
  `ai_auto_analyze` VARCHAR(20) NOT NULL DEFAULT 'open',
  `task_template_share` VARCHAR(20) NOT NULL DEFAULT 'open' COMMENT '共享模板开关',
  `department_owner_view` VARCHAR(20) NOT NULL DEFAULT 'open' COMMENT '部门负责人视角可见开关',
  `user_simple` VARCHAR(255) NULL DEFAULT '' COMMENT '成员总数|1,2,3',
  `dialog_id` BIGINT NULL DEFAULT 0 COMMENT '聊天会话ID',
  `archived_at` TIMESTAMP NULL DEFAULT NULL COMMENT '归档时间',
  `archived_userid` BIGINT NULL DEFAULT 0 COMMENT '归档会员',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  `deleted_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `projects_userid_index` (`userid`),
  KEY `projects_dialog_id_index` (`dialog_id`),
  KEY `projects_archive_method_index` (`archive_method`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `settings` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `name` VARCHAR(100) NULL DEFAULT '',
  `desc` VARCHAR(255) NULL DEFAULT '' COMMENT '参数描述、备注',
  `setting` LONGTEXT NULL DEFAULT NULL,
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `pre_setting_title_index` (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `tmps` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `name` VARCHAR(500) NULL DEFAULT NULL,
  `value` VARCHAR(255) NULL DEFAULT NULL,
  `content` LONGTEXT NULL DEFAULT NULL,
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `pre_tmp_title_index` (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `users` (
  `userid` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `identity` VARCHAR(255) NULL DEFAULT '' COMMENT '身份',
  `department` VARCHAR(255) NULL DEFAULT '' COMMENT '所属部门',
  `az` VARCHAR(10) NULL DEFAULT '' COMMENT 'A-Z',
  `pinyin` VARCHAR(255) NULL DEFAULT '' COMMENT '拼音（主要用于搜索）',
  `email` VARCHAR(100) NULL DEFAULT '' COMMENT '邮箱',
  `tel` VARCHAR(50) NULL DEFAULT '' COMMENT '联系电话',
  `nickname` VARCHAR(255) NULL DEFAULT '' COMMENT '昵称',
  `profession` VARCHAR(255) NULL DEFAULT '' COMMENT '职位/职称',
  `birthday` DATE NULL DEFAULT NULL,
  `address` VARCHAR(255) NULL DEFAULT NULL,
  `introduction` TEXT NULL DEFAULT NULL,
  `userimg` VARCHAR(255) NULL DEFAULT '' COMMENT '头像',
  `encrypt` VARCHAR(50) NULL DEFAULT '',
  `password` VARCHAR(50) NULL DEFAULT '' COMMENT '登录密码',
  `changepass` TINYINT NULL DEFAULT 0 COMMENT '登录需要修改密码',
  `login_num` INT NULL DEFAULT 0 COMMENT '累计登录次数',
  `last_ip` VARCHAR(20) NULL DEFAULT '' COMMENT '最后登录IP',
  `last_at` TIMESTAMP NULL DEFAULT NULL COMMENT '最后登录时间',
  `line_ip` VARCHAR(20) NULL DEFAULT '' COMMENT '最后在线IP（接口）',
  `line_at` TIMESTAMP NULL DEFAULT NULL COMMENT '最后在线时间（接口）',
  `task_dialog_id` BIGINT NULL DEFAULT 0 COMMENT '最后打开的任务会话ID',
  `created_ip` VARCHAR(20) NULL DEFAULT '' COMMENT '注册IP',
  `disable_at` TIMESTAMP NULL DEFAULT NULL COMMENT '禁用时间（离职时间）',
  `email_verity` TINYINT(1) NULL DEFAULT 0 COMMENT '邮箱是否已验证',
  `bot` TINYINT NULL DEFAULT 0 COMMENT '是否机器人',
  `lang` VARCHAR(20) NULL DEFAULT '' COMMENT '语言首选项',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`userid`),
  UNIQUE KEY `users_email_unique` (`email`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `web_socket_dialog_msg_reads` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `dialog_id` BIGINT NULL DEFAULT 0 COMMENT '对话ID',
  `msg_id` BIGINT NULL DEFAULT 0 COMMENT '消息ID',
  `userid` BIGINT NULL DEFAULT 0 COMMENT '接收会员ID',
  `mention` INT NULL DEFAULT 0 COMMENT '是否提及（被@）',
  `silence` INT NULL DEFAULT 0 COMMENT '是否免打扰：0否，1是',
  `email` INT NULL DEFAULT 0 COMMENT '是否发了邮件',
  `after` INT NULL DEFAULT 0 COMMENT '在阅读之后才添加的记录',
  `dot` INT NULL DEFAULT 0 COMMENT '红点标记',
  `read_at` TIMESTAMP NULL DEFAULT NULL COMMENT '阅读时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `web_socket_dialog_msg_reads_msg_id_userid_unique` (`msg_id`, `userid`),
  KEY `web_socket_dialog_msg_reads_userid_read_at_dialog_id_index` (`userid`, `read_at`, `dialog_id`),
  KEY `web_socket_dialog_msg_reads_dialog_id_index` (`dialog_id`),
  KEY `IDEX_dialog_id_userid_read_at_msg_id` (`dialog_id`, `userid`, `read_at`, `msg_id`),
  KEY `idx_ws_msg_reads_userid_silence_read_at` (`userid`, `silence`, `read_at`),
  KEY `idx_ws_msg_reads_dialog_user_mention_read_at` (`dialog_id`, `userid`, `mention`, `read_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `web_socket_dialog_msgs` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `dialog_id` BIGINT NULL DEFAULT 0 COMMENT '对话ID',
  `dialog_type` VARCHAR(50) NULL DEFAULT '' COMMENT '对话类型',
  `session_id` BIGINT NULL DEFAULT 0 COMMENT '会话ID',
  `userid` BIGINT NULL DEFAULT 0 COMMENT '发送会员ID',
  `type` VARCHAR(50) NULL DEFAULT '' COMMENT '消息类型',
  `mtype` VARCHAR(20) NULL DEFAULT '' COMMENT '消息类型（用于搜索）',
  `msg` LONGTEXT NULL DEFAULT NULL COMMENT '详细消息',
  `emoji` LONGTEXT NULL DEFAULT NULL COMMENT 'emoji回复',
  `key` TEXT NULL DEFAULT '' COMMENT '搜索关键词',
  `read` INT NULL DEFAULT 0 COMMENT '已阅数量',
  `send` INT NULL DEFAULT 0 COMMENT '发送数量',
  `tag` BIGINT NULL DEFAULT 0 COMMENT '标注会员ID',
  `todo` BIGINT NULL DEFAULT 0 COMMENT '设为待办会员ID',
  `link` INT NULL DEFAULT 0 COMMENT '是否存在链接',
  `modify` INT NULL DEFAULT 0 COMMENT '是否编辑',
  `bot` TINYINT NULL DEFAULT 0 COMMENT '是否机器人的消息',
  `reply_num` BIGINT NULL DEFAULT 0 COMMENT '有多少条回复',
  `reply_id` BIGINT NULL DEFAULT 0 COMMENT '回复ID',
  `forward_id` BIGINT NULL DEFAULT 0 COMMENT '转发ID',
  `forward_num` BIGINT NULL DEFAULT 0 COMMENT '被转发多少次',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  `deleted_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `web_socket_dialog_msgs_dialog_id_index` (`dialog_id`),
  KEY `web_socket_dialog_msgs_dialog_type_index` (`dialog_type`),
  KEY `web_socket_dialog_msgs_userid_index` (`userid`),
  KEY `web_socket_dialog_msgs_deleted_at_index` (`deleted_at`),
  KEY `web_socket_dialog_msgs_dialog_id_deleted_at_index` (`dialog_id`, `deleted_at`),
  FULLTEXT KEY `web_socket_dialog_msgs_key_fulltext` (`key`),
  KEY `web_socket_dialog_msgs_bot_index` (`bot`),
  KEY `web_socket_dialog_msgs_session_id_index` (`session_id`),
  KEY `web_socket_dialog_msgs_dialog_id_deleted_at_id_index` (`dialog_id`, `deleted_at`, `id`),
  KEY `web_socket_dialog_msgs_reply_id_deleted_at_index` (`reply_id`, `deleted_at`),
  KEY `idx_dialog_file_lists` (`type`, `deleted_at`, `id`, `dialog_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `web_socket_dialog_users` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `dialog_id` BIGINT NULL DEFAULT 0 COMMENT '对话ID',
  `userid` BIGINT NULL DEFAULT 0 COMMENT '会员ID',
  `role` TINYINT NOT NULL DEFAULT 0 COMMENT '0=普通成员 1=群主 2=群管理员',
  `bot` TINYINT NULL DEFAULT 0 COMMENT '是否机器人',
  `top_at` TIMESTAMP NULL DEFAULT NULL COMMENT '置顶时间',
  `last_at` TIMESTAMP NULL DEFAULT NULL COMMENT '最后消息时间',
  `mark_unread` INT NULL DEFAULT 0 COMMENT '是否标记为未读：0否，1是',
  `silence` INT NULL DEFAULT 0 COMMENT '是否免打扰：0否，1是',
  `hide` INT NULL DEFAULT 0 COMMENT '不显示会话：0否，1是',
  `inviter` BIGINT NULL DEFAULT 0 COMMENT '邀请人',
  `important` INT NULL DEFAULT 0 COMMENT '是否不可移出（项目、任务、部门人员）',
  `color` VARCHAR(20) NULL DEFAULT '' COMMENT '颜色',
  `created_at` TIMESTAMP(3) NULL DEFAULT NULL,
  `updated_at` TIMESTAMP(3) NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `web_socket_dialog_users_dialog_id_index` (`dialog_id`),
  KEY `web_socket_dialog_users_userid_dialog_id_index` (`userid`, `dialog_id`),
  KEY `web_socket_dialog_users_bot_index` (`bot`),
  KEY `idx_dialog_role` (`dialog_id`, `role`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `web_socket_dialogs` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `type` VARCHAR(50) NULL DEFAULT '' COMMENT '对话类型',
  `group_type` VARCHAR(50) NULL DEFAULT '' COMMENT '聊天室类型',
  `session_id` BIGINT NULL DEFAULT 0 COMMENT '会话ID',
  `name` VARCHAR(255) NULL DEFAULT '' COMMENT '对话名称',
  `avatar` VARCHAR(255) NULL DEFAULT '' COMMENT '头像（群）',
  `owner_id` BIGINT NULL DEFAULT 0 COMMENT '群主用户ID',
  `link_id` BIGINT NULL DEFAULT 0 COMMENT '关联id',
  `top_userid` BIGINT NULL DEFAULT 0 COMMENT '置顶的用户ID',
  `top_msg_id` BIGINT NULL DEFAULT 0 COMMENT '置顶的消息ID',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  `deleted_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `web_socket_dialogs_owner_id_index` (`owner_id`),
  KEY `web_socket_dialogs_link_id_index` (`link_id`),
  KEY `web_socket_dialogs_deleted_at_index` (`deleted_at`),
  KEY `web_socket_dialogs_session_id_index` (`session_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `web_socket_tmp_msgs` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `md5` VARCHAR(50) NULL DEFAULT '' COMMENT 'MD5(会员ID-消息)',
  `msg` LONGTEXT NULL DEFAULT NULL COMMENT '详细消息',
  `send` TINYINT NULL DEFAULT 0 COMMENT '是否已发送',
  `create_id` BIGINT NULL DEFAULT 0 COMMENT '所属会员ID',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `pre_ws_tmp_msgs_md5_unique` (`md5`),
  KEY `pre_ws_tmp_msgs_send_index` (`send`),
  KEY `pre_ws_tmp_msgs_create_id_index` (`create_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `web_sockets` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `key` VARCHAR(50) NOT NULL DEFAULT '',
  `fd` VARCHAR(50) NULL DEFAULT '',
  `path` VARCHAR(255) NULL DEFAULT '',
  `platform` VARCHAR(20) NULL DEFAULT '' COMMENT '平台类型：android, ios, win, mac, web',
  `userid` BIGINT NULL DEFAULT 0,
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `pre_ws_key_unique` (`key`),
  KEY `pre_ws_userid_index` (`userid`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `file_users` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `file_id` BIGINT NULL DEFAULT 0 COMMENT '项目ID',
  `userid` BIGINT NULL DEFAULT 0 COMMENT '成员ID',
  `permission` INT NULL DEFAULT 0 COMMENT '权限：0只读，1读写',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `file_users_file_id_index` (`file_id`),
  KEY `file_users_userid_index` (`userid`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `project_invites` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `project_id` BIGINT NULL DEFAULT 0 COMMENT '项目ID',
  `num` INT NULL DEFAULT 0 COMMENT '累计邀请',
  `code` VARCHAR(255) NULL DEFAULT '' COMMENT '链接码',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `file_links` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `file_id` BIGINT NULL DEFAULT 0 COMMENT '文件ID',
  `num` INT NULL DEFAULT 0 COMMENT '累计访问',
  `code` VARCHAR(255) NULL DEFAULT '' COMMENT '链接码',
  `userid` BIGINT NULL DEFAULT 0 COMMENT '会员ID',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `file_links_file_id_index` (`file_id`),
  KEY `file_links_userid_index` (`userid`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `reports` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  `title` VARCHAR(255) NOT NULL DEFAULT '' COMMENT '标题',
  `type` ENUM('weekly','daily') NOT NULL DEFAULT 'daily' COMMENT '汇报类型',
  `userid` BIGINT UNSIGNED NOT NULL DEFAULT 0,
  `content` LONGTEXT NULL DEFAULT NULL,
  `sign` VARCHAR(255) NOT NULL DEFAULT '' COMMENT '汇报唯一标识',
  PRIMARY KEY (`id`),
  KEY `reports_userid_created_at_index` (`userid`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `report_receives` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `rid` INT UNSIGNED NOT NULL DEFAULT 0,
  `receive_at` TIMESTAMP NULL DEFAULT NULL COMMENT '接收时间',
  `userid` BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '接收人',
  `read` TINYINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '是否已读',
  PRIMARY KEY (`id`),
  KEY `report_receives_userid_receive_time_index` (`userid`, `receive_at`),
  KEY `report_receives_userid_read_rid_index` (`userid`, `read`, `rid`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `project_flow_items` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `project_id` BIGINT NULL DEFAULT 0 COMMENT '项目ID',
  `flow_id` BIGINT NULL DEFAULT 0 COMMENT '流程ID',
  `name` VARCHAR(50) NULL DEFAULT '' COMMENT '名称',
  `status` VARCHAR(20) NULL DEFAULT '' COMMENT '状态',
  `color` VARCHAR(20) NULL DEFAULT '' COMMENT '自定义颜色',
  `turns` VARCHAR(255) NULL DEFAULT '' COMMENT '可流转',
  `userids` VARCHAR(255) NULL DEFAULT '' COMMENT '状态负责人ID',
  `usertype` VARCHAR(10) NULL DEFAULT '' COMMENT '流转模式',
  `userlimit` TINYINT NULL DEFAULT 0 COMMENT '限制负责人',
  `columnid` BIGINT NULL DEFAULT 0 COMMENT '对应的项目列表',
  `sort` INT NULL DEFAULT NULL COMMENT '排序',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `project_flows` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `project_id` BIGINT NULL DEFAULT 0 COMMENT '项目ID',
  `name` VARCHAR(50) NULL DEFAULT '' COMMENT '流程名称',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `project_task_flow_changes` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `task_id` BIGINT NULL DEFAULT 0 COMMENT '任务ID',
  `userid` BIGINT NULL DEFAULT 0 COMMENT '会员ID',
  `before_flow_item_id` BIGINT NULL DEFAULT 0 COMMENT '（变化前）工作流状态ID',
  `before_flow_item_name` VARCHAR(50) NULL DEFAULT '' COMMENT '（变化前）工作流状态名称',
  `after_flow_item_id` BIGINT NULL DEFAULT 0 COMMENT '（变化后）工作流状态ID',
  `after_flow_item_name` VARCHAR(50) NULL DEFAULT '' COMMENT '（变化后）工作流状态名称',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `project_task_flow_changes_userid_index` (`userid`),
  KEY `project_task_flow_changes_task_id_index` (`task_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `user_email_verifications` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `userid` BIGINT NULL DEFAULT 0 COMMENT '用户id',
  `code` VARCHAR(255) NULL DEFAULT '' COMMENT '验证参数',
  `email` VARCHAR(255) NULL DEFAULT '' COMMENT '电子邮箱',
  `status` INT NULL DEFAULT 0 COMMENT '0-未验证，1-已验证',
  `type` TINYINT NULL DEFAULT 1 COMMENT '邮件类型：1-邮箱认证，2-修改邮箱',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `user_transfers` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `original_userid` BIGINT NULL DEFAULT 0 COMMENT '原作者',
  `new_userid` BIGINT NULL DEFAULT 0 COMMENT '交接人',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `umeng_alias` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `userid` BIGINT NULL DEFAULT 0 COMMENT '会员ID',
  `alias` VARCHAR(50) NULL DEFAULT '' COMMENT '别名',
  `platform` VARCHAR(50) NULL DEFAULT '' COMMENT '平台类型',
  `device` VARCHAR(100) NULL DEFAULT '' COMMENT '设备类型',
  `device_hash` VARCHAR(255) NULL DEFAULT NULL COMMENT '设备哈希值，用于关联UserDevice表',
  `version` VARCHAR(50) NULL DEFAULT '' COMMENT '应用版本号',
  `ua` TEXT NULL DEFAULT NULL COMMENT 'userAgent',
  `is_notified` TINYINT NULL DEFAULT 0 COMMENT '通知权限',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `umeng_alias_device_hash_index` (`device_hash`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `meetings` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `meetingid` VARCHAR(255) NULL DEFAULT '' COMMENT '会议ID，不是数字',
  `name` VARCHAR(255) NULL DEFAULT '' COMMENT '会议主题',
  `channel` VARCHAR(255) NULL DEFAULT '' COMMENT '频道',
  `userid` BIGINT NULL DEFAULT 0 COMMENT '创建人',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  `end_at` TIMESTAMP NULL DEFAULT NULL,
  `deleted_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `meetings_meetingid_unique` (`meetingid`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `web_socket_dialog_msg_todos` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `dialog_id` BIGINT NULL DEFAULT 0 COMMENT '对话ID',
  `msg_id` BIGINT NULL DEFAULT 0 COMMENT '消息ID',
  `userid` BIGINT NULL DEFAULT 0 COMMENT '接收会员ID',
  `done_at` TIMESTAMP NULL DEFAULT NULL COMMENT '完成时间',
  `remind_at` TIMESTAMP NULL DEFAULT NULL COMMENT '提醒时间',
  `reminded_at` TIMESTAMP NULL DEFAULT NULL COMMENT '已提醒时间',
  PRIMARY KEY (`id`),
  KEY `web_socket_dialog_msg_todos_dialog_id_userid_done_at_index` (`dialog_id`, `userid`, `done_at`),
  KEY `web_socket_dialog_msg_todos_msg_id_index` (`msg_id`),
  KEY `web_socket_dialog_msg_todos_userid_index` (`userid`),
  KEY `idx_todo_remind` (`remind_at`, `reminded_at`, `done_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `user_deletes` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `operator` BIGINT NULL DEFAULT 0 COMMENT '操作人员',
  `userid` BIGINT NULL DEFAULT 0 COMMENT '用户id',
  `email` VARCHAR(100) NULL DEFAULT '' COMMENT '邮箱帐号',
  `reason` TEXT NULL DEFAULT NULL COMMENT '注销原因',
  `cache` TEXT NULL DEFAULT '' COMMENT '会员资料缓存',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `project_task_push_logs` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `userid` BIGINT NULL DEFAULT 0 COMMENT '用户id',
  `task_id` INT NULL DEFAULT 0 COMMENT '任务id',
  `type` TINYINT NULL DEFAULT 0 COMMENT '提醒类型：0 任务开始提醒，1 距离到期提醒，2到期超时提醒',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  `deleted_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `project_task_push_logs_userid_index` (`userid`),
  KEY `project_task_push_logs_task_id_index` (`task_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `task_workers` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `args` LONGTEXT NULL DEFAULT NULL,
  `error` LONGTEXT NULL DEFAULT NULL,
  `start_at` TIMESTAMP NULL DEFAULT NULL COMMENT '开始时间',
  `end_at` TIMESTAMP NULL DEFAULT NULL COMMENT '结束时间',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  `deleted_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `task_workers_deleted_at_index` (`deleted_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `user_departments` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `name` VARCHAR(100) NULL DEFAULT '' COMMENT '部门名称',
  `dialog_id` BIGINT NULL DEFAULT 0 COMMENT '聊天会话ID',
  `parent_id` BIGINT NULL DEFAULT 0 COMMENT '上级部门',
  `owner_userid` BIGINT NULL DEFAULT 0 COMMENT '部门负责人',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `user_checkin_macs` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `userid` BIGINT NULL DEFAULT 0 COMMENT '会员id',
  `mac` VARCHAR(100) NULL DEFAULT '' COMMENT 'MAC地址',
  `remark` VARCHAR(100) NULL DEFAULT '' COMMENT '备注',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `user_checkin_records` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `userid` BIGINT NULL DEFAULT 0 COMMENT '会员id',
  `mac` VARCHAR(100) NULL DEFAULT '' COMMENT 'MAC地址',
  `date` VARCHAR(20) NULL DEFAULT '' COMMENT '签到日期',
  `times` TEXT NULL DEFAULT NULL COMMENT '签到时间',
  `report_time` BIGINT NULL DEFAULT 0 COMMENT '上报的时间戳',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_user_checkin_records_userid_mac_date` (`userid`, `mac`, `date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `user_bots` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `userid` BIGINT NULL DEFAULT 0 COMMENT '所属人ID',
  `bot_id` BIGINT NULL DEFAULT 0 COMMENT '机器人ID',
  `clear_day` SMALLINT NULL DEFAULT 90 COMMENT '消息自动清理天数',
  `clear_at` TIMESTAMP NULL DEFAULT NULL COMMENT '下一次清理时间',
  `webhook_url` VARCHAR(255) NULL DEFAULT '' COMMENT '消息webhook地址',
  `webhook_num` INT NULL DEFAULT 0 COMMENT '消息webhook请求次数',
  `webhook_events` TEXT NULL DEFAULT NULL COMMENT 'Webhook事件配置',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `deleteds` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `type` VARCHAR(50) NULL DEFAULT '' COMMENT '删除的数据类型（如：project、task、dialog）',
  `did` BIGINT NULL DEFAULT 0 COMMENT '删除的数据ID',
  `userid` BIGINT NULL DEFAULT 0 COMMENT '关系会员ID',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `deleteds_type_did_userid_unique` (`type`, `did`, `userid`),
  KEY `deleteds_type_userid_index` (`type`, `userid`),
  KEY `deleteds_type_userid_created_at_index` (`type`, `userid`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `approve_proc_msgs` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `proc_inst_id` BIGINT NULL DEFAULT 0 COMMENT '流程实例ID',
  `userid` BIGINT NULL DEFAULT 0 COMMENT '会员ID',
  `msg_id` BIGINT NULL DEFAULT 0 COMMENT '消息ID',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `project_permissions` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `project_id` BIGINT NULL DEFAULT 0 COMMENT '项目ID',
  `permissions` TEXT NULL DEFAULT NULL COMMENT '权限',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `project_task_visibility_users` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `project_id` BIGINT NULL DEFAULT 0 COMMENT '项目ID',
  `task_id` BIGINT NULL DEFAULT 0 COMMENT '任务ID',
  `userid` BIGINT NULL DEFAULT 0 COMMENT '成员ID',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `project_task_visibility_users_project_id_index` (`project_id`),
  KEY `project_task_visibility_users_task_id_index` (`task_id`),
  KEY `project_task_visibility_users_userid_index` (`userid`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `meeting_msgs` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `meetingid` VARCHAR(255) NULL DEFAULT '' COMMENT '会议ID',
  `dialog_id` BIGINT NULL DEFAULT 0 COMMENT '对话ID',
  `msg_id` BIGINT NULL DEFAULT 0 COMMENT '消息ID',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `complaints` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `dialog_id` BIGINT NULL DEFAULT 0 COMMENT '对话ID',
  `userid` BIGINT NULL DEFAULT 0 COMMENT '举报人id',
  `type` BIGINT NULL DEFAULT 0 COMMENT '举报类型',
  `reason` VARCHAR(500) NULL DEFAULT '' COMMENT '举报原因',
  `imgs` TEXT NULL DEFAULT NULL COMMENT '举报图片',
  `status` BIGINT NULL DEFAULT 0 COMMENT '状态 0待处理、1已处理、2已删除',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `user_checkin_faces` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `userid` BIGINT NULL DEFAULT 0 COMMENT '会员id',
  `faceimg` VARCHAR(255) NULL DEFAULT '' COMMENT '人脸图片',
  `status` INT NULL DEFAULT 0 COMMENT '状态',
  `remark` VARCHAR(100) NULL DEFAULT '' COMMENT '备注',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `web_socket_dialog_msg_translates` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `dialog_id` BIGINT NULL DEFAULT 0 COMMENT '对话ID',
  `msg_id` BIGINT NULL DEFAULT 0 COMMENT '消息ID',
  `language` VARCHAR(50) NULL DEFAULT '' COMMENT '语言',
  `content` LONGTEXT NULL DEFAULT NULL COMMENT '翻译内容',
  PRIMARY KEY (`id`),
  KEY `web_socket_dialog_msg_translates_msg_id_language_index` (`msg_id`, `language`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `project_task_templates` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `project_id` BIGINT UNSIGNED NOT NULL COMMENT '项目ID',
  `name` VARCHAR(100) NOT NULL COMMENT '模板名称',
  `title` VARCHAR(255) NULL DEFAULT NULL COMMENT '任务标题',
  `content` TEXT NULL DEFAULT NULL COMMENT '任务内容',
  `sort` TINYINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '排序',
  `is_default` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否默认模板',
  `use_count` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '累计使用次数',
  `last_used_at` TIMESTAMP NULL DEFAULT NULL COMMENT '最近一次使用时间',
  `userid` BIGINT UNSIGNED NOT NULL COMMENT '创建人',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `project_task_templates_project_id_index` (`project_id`),
  KEY `project_task_templates_userid_index` (`userid`),
  KEY `idx_template_usage` (`use_count`, `last_used_at`),
  CONSTRAINT `project_task_templates_project_id_foreign` FOREIGN KEY (`project_id`) REFERENCES `projects` (`id`) ON DELETE CASCADE ON UPDATE RESTRICT,
  CONSTRAINT `project_task_templates_userid_foreign` FOREIGN KEY (`userid`) REFERENCES `users` (`userid`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `web_socket_dialog_configs` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `dialog_id` BIGINT UNSIGNED NOT NULL COMMENT '对话ID',
  `userid` BIGINT UNSIGNED NOT NULL COMMENT '用户ID',
  `type` VARCHAR(50) NOT NULL DEFAULT '' COMMENT '配置类型',
  `value` TEXT NULL DEFAULT NULL COMMENT '配置值',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `web_socket_dialog_configs_dialog_id_index` (`dialog_id`),
  KEY `web_socket_dialog_configs_userid_index` (`userid`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `project_tags` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `project_id` BIGINT UNSIGNED NOT NULL COMMENT '项目ID',
  `name` VARCHAR(50) NOT NULL COMMENT '标签名称',
  `desc` VARCHAR(255) NULL DEFAULT NULL COMMENT '标签描述',
  `color` VARCHAR(20) NULL DEFAULT '' COMMENT '颜色',
  `sort` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '排序',
  `userid` BIGINT UNSIGNED NOT NULL COMMENT '创建人',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `project_tags_project_id_index` (`project_id`),
  KEY `project_tags_userid_index` (`userid`),
  CONSTRAINT `project_tags_project_id_foreign` FOREIGN KEY (`project_id`) REFERENCES `projects` (`id`) ON DELETE CASCADE ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `web_socket_dialog_sessions` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `dialog_id` BIGINT UNSIGNED NOT NULL COMMENT '对话ID',
  `title` VARCHAR(255) NOT NULL DEFAULT '' COMMENT '会话标题',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `web_socket_dialog_sessions_dialog_id_index` (`dialog_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `report_links` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `rid` BIGINT NULL DEFAULT 0 COMMENT '报告ID',
  `num` INT NULL DEFAULT 0 COMMENT '累计访问',
  `code` VARCHAR(255) NULL DEFAULT '' COMMENT '链接码',
  `userid` BIGINT NULL DEFAULT 0 COMMENT '会员ID',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `report_links_rid_index` (`rid`),
  KEY `report_links_userid_index` (`userid`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `report_ai_analyses` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `rid` BIGINT UNSIGNED NOT NULL COMMENT '报告ID',
  `userid` BIGINT UNSIGNED NOT NULL COMMENT '生成分析的会员ID',
  `model` VARCHAR(255) NOT NULL DEFAULT '' COMMENT '使用的模型名称',
  `analysis_text` LONGTEXT NOT NULL COMMENT 'AI 分析的原始文本（Markdown）',
  `meta` JSON NULL DEFAULT NULL COMMENT '额外的上下文信息',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_report_ai_analysis_rid_userid` (`rid`, `userid`),
  KEY `idx_report_ai_analysis_user_updated` (`userid`, `updated_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `user_devices` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `userid` BIGINT NULL DEFAULT 0 COMMENT '会员ID',
  `hash` VARCHAR(255) NULL DEFAULT '' COMMENT 'TOKEN MD5',
  `detail` LONGTEXT NULL DEFAULT NULL COMMENT '详细信息',
  `expired_at` TIMESTAMP NULL DEFAULT NULL COMMENT '过期时间',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  `deleted_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `user_devices_userid_index` (`userid`),
  KEY `user_devices_hash_index` (`hash`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `user_task_browses` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `userid` BIGINT NULL DEFAULT 0 COMMENT '用户ID',
  `task_id` BIGINT NULL DEFAULT 0 COMMENT '任务ID',
  `browsed_at` TIMESTAMP NULL DEFAULT NULL COMMENT '浏览时间',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `user_task_browses_userid_index` (`userid`),
  KEY `user_task_browses_task_id_index` (`task_id`),
  KEY `user_task_browses_browsed_at_index` (`browsed_at`),
  KEY `user_task_browses_userid_browsed_at_index` (`userid`, `browsed_at`),
  UNIQUE KEY `user_task_browses_userid_task_id_unique` (`userid`, `task_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `user_recent_items` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `userid` BIGINT NOT NULL DEFAULT 0 COMMENT '用户ID',
  `target_type` VARCHAR(50) NOT NULL DEFAULT '' COMMENT '目标类型(task/file/task_file/message_file 等)',
  `target_id` BIGINT NOT NULL DEFAULT 0 COMMENT '目标ID',
  `source_type` VARCHAR(50) NOT NULL DEFAULT '' COMMENT '来源类型(project/filesystem/project_task/dialog 等)',
  `source_id` BIGINT NOT NULL DEFAULT 0 COMMENT '来源ID',
  `browsed_at` TIMESTAMP NULL DEFAULT NULL COMMENT '浏览时间',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `user_recent_items_userid_index` (`userid`),
  KEY `user_recent_items_browsed_at_index` (`browsed_at`),
  KEY `user_recent_items_userid_browsed_at_index` (`userid`, `browsed_at`),
  UNIQUE KEY `recent_unique` (`userid`, `target_type`, `target_id`, `source_type`, `source_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `user_favorites` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `userid` BIGINT NULL DEFAULT 0 COMMENT '用户ID',
  `favoritable_type` VARCHAR(50) NULL DEFAULT '' COMMENT '收藏类型(比如：task/project/file/message)',
  `favoritable_id` BIGINT NULL DEFAULT 0 COMMENT '收藏对象ID',
  `remark` VARCHAR(255) NOT NULL DEFAULT '' COMMENT '收藏备注',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `user_favorites_userid_index` (`userid`),
  KEY `user_favorites_favoritable_type_index` (`favoritable_type`),
  KEY `user_favorites_favoritable_id_index` (`favoritable_id`),
  KEY `user_favorites_userid_favoritable_type_index` (`userid`, `favoritable_type`),
  UNIQUE KEY `user_favorites_unique` (`userid`, `favoritable_type`, `favoritable_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `project_task_relations` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `task_id` BIGINT UNSIGNED NOT NULL COMMENT '任务ID',
  `related_task_id` BIGINT UNSIGNED NOT NULL COMMENT '关联任务ID',
  `direction` VARCHAR(32) NOT NULL DEFAULT 'mention' COMMENT '关系方向: mention/mentioned_by',
  `dialog_id` BIGINT UNSIGNED NULL DEFAULT NULL COMMENT '来源会话ID',
  `msg_id` BIGINT UNSIGNED NULL DEFAULT NULL COMMENT '来源消息ID',
  `userid` BIGINT UNSIGNED NULL DEFAULT NULL COMMENT '提及人',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `project_task_relations_unique` (`task_id`, `related_task_id`, `direction`),
  KEY `project_task_relations_task_id_direction_index` (`task_id`, `direction`),
  KEY `project_task_relations_related_task_id_index` (`related_task_id`),
  KEY `project_task_relations_dialog_id_index` (`dialog_id`),
  KEY `project_task_relations_msg_id_index` (`msg_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `user_tags` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `user_id` BIGINT UNSIGNED NOT NULL COMMENT '被标签用户ID',
  `name` VARCHAR(50) NOT NULL COMMENT '标签名称',
  `created_by` BIGINT UNSIGNED NOT NULL COMMENT '创建人',
  `updated_by` BIGINT UNSIGNED NULL DEFAULT NULL COMMENT '最后更新人',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `user_tags_user_id_index` (`user_id`),
  KEY `user_tags_created_by_index` (`created_by`),
  UNIQUE KEY `user_tags_unique_name` (`user_id`, `name`),
  KEY `user_tags_updated_by_foreign` (`updated_by`),
  CONSTRAINT `user_tags_user_id_foreign` FOREIGN KEY (`user_id`) REFERENCES `users` (`userid`) ON DELETE CASCADE ON UPDATE RESTRICT,
  CONSTRAINT `user_tags_created_by_foreign` FOREIGN KEY (`created_by`) REFERENCES `users` (`userid`) ON DELETE CASCADE ON UPDATE RESTRICT,
  CONSTRAINT `user_tags_updated_by_foreign` FOREIGN KEY (`updated_by`) REFERENCES `users` (`userid`) ON DELETE SET NULL ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `user_tag_recognitions` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `tag_id` BIGINT UNSIGNED NOT NULL COMMENT '标签ID',
  `user_id` BIGINT UNSIGNED NOT NULL COMMENT '认可人ID',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `user_tag_recognitions_tag_id_index` (`tag_id`),
  KEY `user_tag_recognitions_user_id_index` (`user_id`),
  UNIQUE KEY `user_tag_recognitions_unique` (`tag_id`, `user_id`),
  CONSTRAINT `user_tag_recognitions_tag_id_foreign` FOREIGN KEY (`tag_id`) REFERENCES `user_tags` (`id`) ON DELETE CASCADE ON UPDATE RESTRICT,
  CONSTRAINT `user_tag_recognitions_user_id_foreign` FOREIGN KEY (`user_id`) REFERENCES `users` (`userid`) ON DELETE CASCADE ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `umeng_logs` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `request` TEXT NULL DEFAULT NULL COMMENT '请求参数',
  `response` TEXT NULL DEFAULT NULL COMMENT '推送返回',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `user_app_sorts` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `userid` BIGINT NOT NULL COMMENT '用户ID',
  `sorts` JSON NULL DEFAULT NULL COMMENT '排序配置',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `user_app_sorts_userid_unique` (`userid`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `manticore_sync_failures` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `data_type` VARCHAR(20) NOT NULL COMMENT '数据类型: msg/file/task/project/user',
  `data_id` BIGINT NOT NULL COMMENT '数据ID',
  `action` VARCHAR(20) NOT NULL COMMENT '操作类型: sync/delete',
  `error_message` VARCHAR(500) NULL DEFAULT NULL COMMENT '错误信息',
  `retry_count` INT NOT NULL DEFAULT 0 COMMENT '重试次数',
  `last_retry_at` TIMESTAMP NULL DEFAULT NULL COMMENT '最后重试时间',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_type_id_action` (`data_type`, `data_id`, `action`),
  KEY `idx_retry` (`last_retry_at`, `retry_count`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `project_task_ai_events` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `task_id` BIGINT NOT NULL COMMENT '任务ID',
  `event_type` VARCHAR(50) NOT NULL COMMENT '事件类型: description/subtasks/assignee/similar',
  `status` VARCHAR(20) NOT NULL DEFAULT 'pending' COMMENT '状态: pending/processing/completed/failed/skipped',
  `retry_count` TINYINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '重试次数',
  `result` JSON NULL DEFAULT NULL COMMENT '执行结果',
  `error` TEXT NULL DEFAULT NULL COMMENT '错误信息',
  `msg_id` BIGINT NULL DEFAULT 0 COMMENT '消息ID',
  `executed_at` TIMESTAMP NULL DEFAULT NULL COMMENT '执行时间',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_task_event` (`task_id`, `event_type`),
  KEY `idx_status` (`status`),
  KEY `idx_created` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `ai_assistant_sessions` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `userid` BIGINT NOT NULL DEFAULT 0 COMMENT '用户ID',
  `session_key` VARCHAR(100) NOT NULL DEFAULT '' COMMENT '场景分类key',
  `session_id` VARCHAR(100) NOT NULL DEFAULT '' COMMENT '前端生成的会话ID',
  `scene_key` VARCHAR(200) NOT NULL DEFAULT '' COMMENT '具体场景标识',
  `title` VARCHAR(255) NOT NULL DEFAULT '' COMMENT '会话标题',
  `data` LONGTEXT NULL DEFAULT NULL COMMENT 'responses JSON',
  `images` LONGTEXT NULL DEFAULT NULL COMMENT '图片映射 {imageId: relativePath}',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_userid` (`userid`),
  UNIQUE KEY `uk_user_session` (`userid`, `session_key`, `session_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `user_department_owners` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `department_id` BIGINT UNSIGNED NOT NULL COMMENT '部门ID',
  `userid` BIGINT UNSIGNED NOT NULL COMMENT '部门管理员 userid',
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_dept_user` (`department_id`, `userid`),
  KEY `idx_userid` (`userid`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `ai_assistant_search_logs` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `userid` BIGINT NOT NULL DEFAULT 0 COMMENT '用户ID（token推导）',
  `dialog_id` BIGINT NOT NULL DEFAULT 0 COMMENT '对话ID（chat流程；invoke流程为0）',
  `context_key` VARCHAR(191) NOT NULL DEFAULT '' COMMENT '上下文标识（chat=插件context_key；invoke=前端session_id）',
  `source` VARCHAR(20) NOT NULL DEFAULT '' COMMENT '来源：chat|invoke',
  `query` VARCHAR(500) NOT NULL DEFAULT '' COMMENT '检索query（截断500）',
  `locale` VARCHAR(10) NOT NULL DEFAULT '' COMMENT '语种 zh|en',
  `source_ids` TEXT NULL DEFAULT NULL COMMENT '命中source id列表 JSON',
  `top_score` DECIMAL(6,4) NOT NULL DEFAULT 0 COMMENT '最高相似度 0-1',
  `result_count` INT NOT NULL DEFAULT 0 COMMENT '命中数量',
  `duration_ms` INT NOT NULL DEFAULT 0 COMMENT '检索耗时毫秒',
  `empty` TINYINT NOT NULL DEFAULT 0 COMMENT '是否空结果 0|1',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_userid` (`userid`),
  KEY `idx_context_key` (`context_key`),
  KEY `idx_empty_created` (`empty`, `created_at`),
  KEY `idx_created_at` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `ai_assistant_feedbacks` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `userid` BIGINT NOT NULL DEFAULT 0 COMMENT '用户ID',
  `session_key` VARCHAR(100) NOT NULL DEFAULT '' COMMENT '场景分类key（同ai_assistant_sessions）',
  `session_id` VARCHAR(100) NOT NULL DEFAULT '' COMMENT '前端会话ID（=检索日志context_key，松关联）',
  `local_id` BIGINT NOT NULL DEFAULT 0 COMMENT '前端回复条目localId',
  `feedback` VARCHAR(10) NOT NULL DEFAULT '' COMMENT 'like|dislike',
  `prompt` TEXT NULL DEFAULT NULL COMMENT '用户问题（截断1000）',
  `answer_digest` VARCHAR(32) NOT NULL DEFAULT '' COMMENT '回复内容md5',
  `answer` TEXT NULL DEFAULT NULL COMMENT '回复摘录（去reasoning截断2000）',
  `source_ids` TEXT NULL DEFAULT NULL COMMENT '回复引用的kb source id列表 JSON',
  `model` VARCHAR(100) NOT NULL DEFAULT '' COMMENT '模型名',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_user_entry` (`userid`, `session_key`, `session_id`, `local_id`),
  KEY `idx_feedback_created` (`feedback`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `app_badges` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `app_id` VARCHAR(100) NOT NULL DEFAULT '' COMMENT '应用ID（appstore 插件 appid 或自定义微应用 id）',
  `menu_key` VARCHAR(100) NOT NULL DEFAULT '' COMMENT '菜单稳定标识；空串表示该应用的第一个菜单',
  `userid` BIGINT NOT NULL COMMENT '用户ID',
  `count` INT NOT NULL DEFAULT 0 COMMENT '角标数字',
  `dot` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否显示红点',
  `updated_at` TIMESTAMP NULL DEFAULT NULL COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `app_badges_unique` (`app_id`, `menu_key`, `userid`),
  KEY `app_badges_userid` (`userid`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `web_socket_dialog_msg_attachments` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `msg_id` BIGINT UNSIGNED NOT NULL COMMENT '消息ID',
  `dialog_id` BIGINT UNSIGNED NOT NULL COMMENT '会话ID',
  `source_type` VARCHAR(20) NOT NULL COMMENT '来源类型：file_message、inline_image',
  `kind` VARCHAR(20) NOT NULL COMMENT '附件类型：file、image',
  `position` SMALLINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '消息内附件顺序',
  `name` VARCHAR(255) NOT NULL DEFAULT '' COMMENT '附件名称',
  `ext` VARCHAR(20) NOT NULL DEFAULT '' COMMENT '文件扩展名',
  `path` TEXT NULL DEFAULT NULL COMMENT '原文件地址',
  `thumb` TEXT NULL DEFAULT NULL COMMENT '缩略图地址',
  `size` BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '文件大小(B)',
  `width` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '图片宽度',
  `height` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '图片高度',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  `cursor_msg_id` BIGINT GENERATED ALWAYS AS (-`msg_id`) STORED,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_ws_msg_attachment_position` (`msg_id`, `source_type`, `position`),
  KEY `idx_ws_msg_attachment_dialog` (`dialog_id`, `id`),
  KEY `idx_ws_msg_attachment_kind` (`kind`, `id`),
  KEY `idx_ws_msg_attachment_cursor` (`cursor_msg_id`, `position`, `id`),
  KEY `idx_ws_msg_attachment_ext_msg` (`ext`, `msg_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `web_socket_dialog_msg_attachment_backfills` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `status` VARCHAR(20) NOT NULL DEFAULT 'pending' COMMENT '状态：pending、running、failed、completed',
  `before_msg_id` BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '启动时消息快照上界',
  `last_msg_id` BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '已处理到的消息ID',
  `processed_count` BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '成功处理消息数',
  `attachment_count` BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '已索引附件数',
  `empty_count` BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '无有效附件消息数',
  `failure_count` BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '失败尝试次数',
  `retry_count` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '当前消息连续重试次数',
  `last_error` TEXT NULL DEFAULT NULL COMMENT '最近一次错误',
  `started_at` TIMESTAMP NULL DEFAULT NULL COMMENT '首次开始时间',
  `completed_at` TIMESTAMP NULL DEFAULT NULL COMMENT '完成时间',
  `next_retry_at` TIMESTAMP NULL DEFAULT NULL COMMENT '下次重试时间',
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_ws_msg_attachment_backfill_pending` (`status`, `next_retry_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `webdav_credentials` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `public_id` VARCHAR(40) NOT NULL,
  `userid` BIGINT NOT NULL,
  `name` VARCHAR(100) NOT NULL,
  `password_hash` VARCHAR(255) NOT NULL,
  `password_suffix` VARCHAR(4) NOT NULL,
  `expires_at` TIMESTAMP NULL DEFAULT NULL,
  `last_used_at` TIMESTAMP NULL DEFAULT NULL,
  `last_used_ip` VARCHAR(45) NULL DEFAULT NULL,
  `last_user_agent` VARCHAR(255) NULL DEFAULT NULL,
  `revoked_at` TIMESTAMP NULL DEFAULT NULL,
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `webdav_credentials_public_id_unique` (`public_id`),
  KEY `webdav_credentials_userid_index` (`userid`),
  KEY `webdav_credentials_expires_at_index` (`expires_at`),
  KEY `webdav_credentials_revoked_at_index` (`revoked_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `webdav_locks` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `token` VARCHAR(100) NOT NULL,
  `userid` BIGINT NOT NULL,
  `credential_id` BIGINT NOT NULL,
  `file_id` BIGINT NULL DEFAULT NULL,
  `uri` VARCHAR(1000) NOT NULL,
  `uri_hash` CHAR(64) NOT NULL,
  `owner` VARCHAR(255) NULL DEFAULT NULL,
  `scope` VARCHAR(20) NOT NULL DEFAULT 'exclusive',
  `depth` VARCHAR(20) NOT NULL DEFAULT 'infinity',
  `timeout_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `webdav_locks_token_unique` (`token`),
  KEY `webdav_locks_userid_index` (`userid`),
  KEY `webdav_locks_credential_id_index` (`credential_id`),
  KEY `webdav_locks_file_id_index` (`file_id`),
  KEY `webdav_locks_uri_hash_index` (`uri_hash`),
  KEY `webdav_locks_timeout_at_index` (`timeout_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `webdav_properties` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `userid` BIGINT NOT NULL,
  `path` VARCHAR(1000) NOT NULL,
  `path_hash` CHAR(64) NOT NULL,
  `name` VARCHAR(255) NOT NULL,
  `property_hash` CHAR(64) NOT NULL,
  `value_type` TINYINT UNSIGNED NOT NULL DEFAULT 1,
  `value` LONGTEXT NULL DEFAULT NULL,
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `webdav_properties_userid_index` (`userid`),
  KEY `webdav_properties_path_hash_index` (`path_hash`),
  UNIQUE KEY `webdav_properties_user_path_property_unique` (`userid`, `path_hash`, `property_hash`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `webdav_operation_logs` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `request_id` VARCHAR(100) NULL DEFAULT NULL,
  `userid` BIGINT NULL DEFAULT NULL,
  `credential_id` BIGINT NULL DEFAULT NULL,
  `method` VARCHAR(20) NOT NULL,
  `uri` VARCHAR(1000) NULL DEFAULT NULL,
  `file_id` BIGINT NULL DEFAULT NULL,
  `status` SMALLINT NOT NULL DEFAULT 0,
  `result` VARCHAR(255) NULL DEFAULT NULL,
  `bytes` BIGINT NOT NULL DEFAULT 0,
  `ip` VARCHAR(45) NULL DEFAULT NULL,
  `user_agent` VARCHAR(255) NULL DEFAULT NULL,
  `duration_ms` INT UNSIGNED NOT NULL DEFAULT 0,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `webdav_operation_logs_request_id_index` (`request_id`),
  KEY `webdav_operation_logs_userid_index` (`userid`),
  KEY `webdav_operation_logs_credential_id_index` (`credential_id`),
  KEY `webdav_operation_logs_file_id_index` (`file_id`),
  KEY `webdav_operation_logs_status_index` (`status`),
  KEY `webdav_operation_logs_created_at_index` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `project_task_handoffs` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `task_id` INT UNSIGNED NOT NULL,
  `userid` INT UNSIGNED NOT NULL DEFAULT 0,
  `source` VARCHAR(32) NOT NULL,
  `record` JSON NOT NULL,
  `created_at` TIMESTAMP NULL DEFAULT NULL,
  `updated_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `project_task_handoffs_task_id_id_index` (`task_id`, `id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
