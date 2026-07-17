-- 0001_init_license_issues.sql
-- 建库（幂等）
CREATE DATABASE IF NOT EXISTS `pic_clear`
  DEFAULT CHARSET utf8mb4
  DEFAULT COLLATE utf8mb4_unicode_ci;

USE `pic_clear`;

-- license 签发流水
CREATE TABLE IF NOT EXISTS `license_issues` (
  `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `fingerprint`   VARCHAR(32)  NOT NULL COMMENT '机器指纹 XXXX-XXXX-XXXX-XXXX',
  `issued_to`     VARCHAR(64)  NOT NULL COMMENT '颁发给谁',
  `expire_date`   VARCHAR(16)  NOT NULL DEFAULT 'never' COMMENT 'never 或 YYYY-MM-DD',
  `note`          VARCHAR(200) NOT NULL DEFAULT '' COMMENT '备注',
  `issued_at`     DATE         NOT NULL COMMENT '签发当天（写进 payload 里的日期）',
  `created_at`    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时刻',
  `source`        VARCHAR(16)  NOT NULL DEFAULT 'web' COMMENT 'web / cli',
  `operator`      VARCHAR(64)  NOT NULL DEFAULT '' COMMENT '操作者，预留',
  `client_ip`     VARCHAR(64)  NOT NULL DEFAULT '' COMMENT '来源 IP',
  `payload_b64`   TEXT         NOT NULL COMMENT 'license.lic 第 1 行',
  `signature_b64` TEXT         NOT NULL COMMENT 'license.lic 第 2 行',
  PRIMARY KEY (`id`),
  KEY `idx_fp` (`fingerprint`),
  KEY `idx_created` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='pic-clear 授权文件签发历史';
