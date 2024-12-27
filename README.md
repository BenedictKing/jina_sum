# jina_sumary
ChatGPT on WeChat项目插件, 使用jina reader和ChatGPT总结网页链接内容

支持总结公众号、小红书、csdn等分享卡片链接(有的卡片链接会触发验证，一般直链没有此问题)

## 功能
- 支持自动总结微信文章
- 支持手动触发总结
- 支持总结后追问文章内容
- 支持群聊和私聊场景

## 使用方法
1. 私聊：
   - 直接发送文章链接，会自动总结
   - 发送"总结 链接"手动触发总结
   - 总结完成后5分钟内可发送"问xxx"追问文章内容

2. 群聊：
   - 发送文章链接后，发送"总结"触发总结
   - 或直接发送"总结 链接"手动触发总结
   - 总结完成后5分钟内可发送"问xxx"追问文章内容

![wechat_mp](./docs/images/wechat_mp.jpg)
![red](./docs/images/red.jpg)
![csdn](./docs/images/csdn.jpg)

## 配置说明
```bash
{
  "jina_reader_base": "https://r.jina.ai",           # jina reader链接，默认为https://r.jina.ai
  "open_ai_api_base": "https://api.openai.com/v1",   # chatgpt chat url
  "open_ai_api_key":  "sk-xxx",                      # chatgpt api key
  "open_ai_model": "gpt-3.5-turbo",                  # chatgpt model
  "max_words": 8000,                                 # 网页链接内容的最大字数，防止超过最大输入token，使用字符串长度简单计数
  "white_url_list": [],                              # url白名单, 列表为空时不做限制，黑名单优先级大于白名单，即当一个url既在白名单又在黑名单时，黑名单生效
  "black_url_list": ["https://support.weixin.qq.com", "https://channels-aladin.wxqcloud.qq.com"],  # url黑名单，排除不支持总结的视频号等链接
  "prompt": "我需要对下面的文本进行总结，总结输出包括以下三个部分：\n📖 一句话总结\n🔑 关键要点,用数字序号列出3-5个文章的核心内容\n🏷 标签: #xx #xx\n请使用emoji让你的表达更生动。",                           # 链接内容总结提示词
  "enabled": true,                                   # 是否启用插件
  "group": true,                                    # 是否在群聊中启用
  "auto_sum": false,                                # 是否自动总结（仅私聊有效）
  "qa_trigger": "问",                               # 追问触发词
  "black_group_list": [],                          # 群聊黑名单
  "summary_cache_timeout": 300,                     # 总结缓存超时时间（秒）
  "content_cache_timeout": 300                      # 内容缓存超时时间（秒）
}
```

## 注意事项
1. 需要配置 OpenAI API Key
2. 群聊中需要@机器人触发总结
3. 追问功能仅在总结完成后5分钟内有效
4. 支持的文章来源：微信公众号、知乎、简书等主流平台

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=hanfangyuan4396/jina_sum&type=Date)](https://star-history.com/#hanfangyuan4396/jina_sum&Date)
