# elasticsearch 8.15.3 + IK 中文分词（app/search 的索引映射用 ik_max_word，
# 官方镜像不带此插件，建索引会 400——2026-10-21 线上实测）
FROM elasticsearch:8.15.3
RUN elasticsearch-plugin install --batch \
    https://release.infinilabs.com/analysis-ik/stable/elasticsearch-analysis-ik-8.15.3.zip \
    && ls /usr/share/elasticsearch/plugins/analysis-ik/plugin-descriptor.properties
