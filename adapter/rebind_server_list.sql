-- 把一花后台的「服务器列表」指向本机 CCProxy 适配层。
-- 关键点：适配层跑在宿主机上，yihua-app 容器只能经 docker 网关（172.20.0.1）访问它，
-- 走公网 IP 会因 NAT 回环失败（已实测 Connection timed out）。
-- server_list.ip 在一花里只用于后台 -> CCProxy 的 fsockopen，不对外展示，
-- 所以填网关地址不影响终端用户。
SET NAMES utf8mb4;

UPDATE server_list
   SET ip         = '172.20.0.1',
       serveruser = 'admin',
       password   = 'Adapter-Token-ChangeMe',
       state      = 1,
       cport      = 8893,
       comment    = '本机CCProxy适配层 systemd:ccpx | 8893=管理API+HTTP代理 | 8892=SOCKS5 | 出口链花瓶8888 | 公网需在安全组放行8892/8893'
 WHERE ip = '203.0.113.10';

UPDATE application
   SET serverip = '172.20.0.1'
 WHERE serverip = '203.0.113.10';

SELECT id, ip, serveruser, cport, state, comment FROM server_list;
SELECT appid, appcode, appname, serverip FROM application;
