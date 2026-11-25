# NLP Pre 汇报讲稿（中英对照）

> 覆盖范围：P2–P12、P15–P19。按你的要求，P1 封面、P13–P14 结果页、P20 致谢页不写讲稿。已按更新后的 PPT 同步：P15 新增两条要点，P18、P19 改为两条新的反思（handoff 后的额外模型调用、重复生成产物）。
>
> 口径提醒：P7–P12 的截图是历史运行中「Done / Pass」的六个任务记录；当前 `delivery-v8` 快照的冻结六题live测量仍是部分完成、未验收（README 记录为 0/6，21m21.804s，含人工等待），不能据此声称整套通过或相对 EvoMAC 有加速。讲的时候把「截图里的通过」说成「当时那次运行的平台验收」，不要说成课程最终验收。

---

## P2 · Dynamic Task Orchestration — Toolkit & Profile

**中文**

先看分工的第一层：能力画像和工具权限。左边这张表是每个 Profile 能用的工具，右边是我们在工作台里编辑角色的界面——勾选能力画像后，工具是自动合并进来的，不能在这里逐项改。Planner 看到用户需求和在席 Agent 的画像，先判断「谁适合做这件事」。比如 Researcher 和 Writer 都能搜网页、读写文件，但一个擅长取证，一个擅长成稿，所以「调查校园 AI 事件再写报道」会先派给 Reporter，再交给 Editor。第二层是工具：Coder 有 code.run / code.test，Release 有 commit / push，任务能不能落地，派单时就要看这张清单。注意两个边界：模型负责判断角色匹配，平台校验负责人和依赖，真正调用工具时网关再查一次权限。还有一个容易混的点——表里的 Planner 画像和一次运行里生成任务图的 Planner 不是一回事；生成图的那次调用工具是关掉的，它只读团队能力表、输出 JSON。所以这张矩阵放在流程图的输入端最合适。

**EN**

So, first layer of orchestration: who's allowed to do what. This matrix shows the tool set for each profile, and the screenshot on the right is our role editor — you tick a capability profile and the tools come along with it; you can't hand-pick them one by one there. The planner looks at the request and the roster, then decides who fits. A researcher and a writer can both search the web and edit files, but one is good at finding evidence and the other at shaping a story, so an "investigate this campus AI event and write it up" job goes to the reporter first, then the editor. The second layer is tools: the coder has run and test, release has commit and push. Whether a subtask is actually executable depends on that checklist. Two boundaries matter here: the model judges the fit, the platform validates assignments and dependencies, and the gateway enforces permissions again at call time. Also, don't confuse the Planner profile in this table with the planner call that produces a task graph — during planning, tools are off; it just reads the roster and emits JSON.

---

## P3 · Dynamic Task Orchestration — Task Dependency Graph & Validation

**中文**

Planner 的输出不是一段文字，而是一张 JSON 任务依赖图，也就是把分工写成可执行的数据。每个节点有六个字段：`agentId` 决定谁做，`instructions` 写做什么，`dependsOn` 写先等谁，`expectedOutput` 写交出什么，`requiresArtifacts` 说明是否必须发布真实文件，`reviewOf` 指明评审针对哪一项。以半导体幻灯片为例：t1 行业研究、t2 投资观点、t3 做 HTML 幻灯片、t4 风险评审；t3 明确依赖 t1 和 t2，t4 依赖 t3 并且 `reviewOf: t3`。提示词会要求 2 到 5 个有效子任务，同一角色能连着做完的工作尽量合并，避免碎成一堆节点。生成之后不是直接跑——`platform.py` 里的 `validate_plan()` 用确定性代码检查：负责人是否在席、任务 ID 是否唯一、依赖是否只指向前序节点。最后这一条顺手排除了循环依赖；评审节点还必须依赖被审任务，且由另一位 Agent 担任。第一次校验失败时，平台把具体错误回给 Planner，允许改一次。通过后调度器每次挑一个上游全部 done 的节点执行。一句话概括两层：角色匹配是模型判断，图结构和评审独立性是代码强制检查。

**EN**

The planner doesn't just write prose — it emits a JSON dependency graph, so the division of labor becomes executable data. Every node has six fields: `agentId` says who, `instructions` says what, `dependsOn` says who has to finish first, `expectedOutput` says what gets handed over, `requiresArtifacts` says whether a real file must be published, and `reviewOf` says which task a verdict applies to. In the semiconductor case, t1 is industry research, t2 is the investment view, t3 builds the HTML deck, and t4 is the risk review — t3 waits on t1 and t2, and t4 waits on t3 with `reviewOf: t3`. The prompt asks for two to five useful subtasks and tells the planner to merge work the same role can do in one go, so we don't end up with ten tiny nodes. The graph isn't run as-is: `validate_plan()` in `platform.py` checks that the assignee is present, IDs are unique, and dependencies only point backwards — which rules out cycles by construction. A review node must depend on the task it reviews and come from a different agent. If validation fails, the platform sends the exact error back and lets the planner fix it once. Then the scheduler picks a node whose upstream tasks are all done. Role matching is the model's call; graph structure and review independence are the code's job.

---

## P4 · Collaboration, Handoffs, and Acceptance Testing

**中文**

这张图回答四个问题：Agent 怎么沟通、依赖怎么传、下游怎么读到真实文件、评审凭什么算数。第一，Agent 用 `collab.message` 同步发现和进度，用 `collab.delegate` 临时把活派给另一位专家；后者会生成带 `parentId` 的子任务，完成后结果回到委派方。第二，依赖不是一句「前面做完了」，而是带回执的：以 t3 为例，只有 t1 和 t2 都变成 done，调度器才启动它，并把两个直接上游的 `taskId`、`agentId` 和简短 `result` 写进 DEPENDENCIES 上下文；上游失败，下游就是 blocked。第三，工作区彼此隔离，所以上游发布产物时平台会保存快照、算 SHA-256，再给指定接收者只读授权。这里有个细节值得指：图上的箭头只表示执行顺序，文件授权表示可读范围——风控虽然只直接依赖 t3，但只要被授权，它可以读 t1 到 t3 三份文件，核对研究、观点和幻灯片是否一致。第四，评审要绑定版本：`reviewOf` 指向 t3，评审者必须是另一个 Agent，还要真的读过哈希匹配的版本；HTML/JS 还得在 Docker 里对当前版本跑通浏览器测试。比如解析 HTML 只能证明按钮存在，真去点一下翻页才算验证行为。评审 fail 时，平台把具体缺陷发回生产者，只针对被打回的产物建返工任务；新版哈希变了，就要重新验收。所以「执行完成」和「当前版本通过验收」是两个独立状态。

**EN**

This slide answers four questions. How do agents talk? They use `collab.message` to share findings, and `collab.delegate` when they actually need another specialist to do work — that creates a child task with a `parentId`, and the result comes back to the delegator. How does a dependency travel downstream? Not as "the previous task is done," but as a receipt. For t3, the scheduler waits until t1 and t2 are both done, then writes their task IDs, agent IDs and short results into the DEPENDENCIES context. If an upstream task fails, downstream tasks become blocked. How does a downstream agent read a real file when workspaces are isolated? Publishing snapshots the bytes, records a SHA-256, and grants named recipients read-only imports. Here's the subtle bit: arrows in the graph are execution order, while grants are read scope. The risk manager only directly depends on t3, but with explicit grants it can read all three files and check that research, investment view and slides agree. And why do we trust a pass? Because `reviewOf` pins the verdict to t3, the reviewer must be a different agent who actually read the matching hashes, and for HTML/JS the current version has to pass browser tests in Docker. Parsing HTML proves a button exists; clicking it proves navigation works. If the verdict fails, only that artifact goes back for repair, and a new hash means a fresh review. Execution done and version accepted are two separate states.

---

## P5 · Human-in-the-Loop

**中文**

这里两条链路都来自真实 run 记录，不是我们假设的流程。第一条是缺关键信息：半导体幻灯片返工时，网页搜索没配 key、抓取又经常 403/404，Agent 拿不到带日期的一手数据，于是调用 `ask_user`，给出两个选项——A 是接受标注为假设的底稿、主来源 URL 标成「未验证指针」，B 是等人提供原文和数字。run 进入 `waiting_human`，停了大概 56 秒，用户选 A；Agent 在同一个返工子任务里继续 patch、发布新版，风险经理再对新的哈希判 pass。第二条是关键操作前审批：营销 Agent 想整文件覆盖 `client_intake_brief.md`，网关拦下来返回 `needsApproval`，待批单绑定了路径、当时的 SHA-256 和待写入全文；用户批准只针对这一次参数，写入后文件哈希变了，再发布、再独立验收，等待大约 55 秒。两条链路的共同点是：平台不替用户编答案，也不自动放行写盘；人工等待计入原本的 wall deadline。区别是输入形态——澄清是文字或选项，审批是 `true` / `false`。

**EN**

Both of these chains come from real run records, not from a slide we imagined. First one: missing information. During the semiconductor slide repair, web search had no API key and fetches kept returning 403 or 404, so the agent couldn't get dated primary sources. It called `ask_user` with two options — A, accept a draft with clearly labelled assumptions and mark the primary URLs as unverified pointers, or B, wait for someone to supply the source text. The run went into `waiting_human` for about 56 seconds, the user picked A, and the agent carried on in the same repair subtask: patch, publish the new version, and the risk manager passed that new hash. Second chain: approval before a risky operation. The marketing agent tried to overwrite `client_intake_brief.md`; the gateway returned `needsApproval` with the exact path, the current hash and the proposed content bound to the request. The user's "yes" applied only to those exact arguments. After the write, the hash changed, then publish and independent review, with roughly 55 seconds of human wait. The pattern is the same in both: the platform never invents an answer and never auto-approves a write, and human waiting still counts against the deadline. The difference is just the input — text or options for clarification, yes or no for approval.

---

## P6 · Memory & Evolution

**中文**

我们的记忆策略可以压缩成三句话。第一，记忆属于「Agent × 频道」，存在各团队的 SQLite 里：谁做的任务谁积累经验，成功、失败、部分完成都会留下情节记忆，并带上 run、task 和工具调用引用；私聊里的经验不会进入群任务上下文。第二，一次成功只是候选策略：子任务完成后按签名提炼，同一签名在两个不同子任务上连续成功，或经过人工确认，才升级为 verified。截图里就能看到一条 verified 和一条 candidate 并存。第三，新任务按需召回：系统用本地词法检索匹配当前目标，不调 embedding API；已验证策略优先，候选会明确标成「未验证」。这里有两个维度要分清：一个是策略本身 verified 还是 candidate，回答「有多少证据支持」；另一个是本次任务只是 Retrieved only，还是通过 `memory.use` 明确声明 Applied。所以一条策略可能既是 verified 又是 retrieved-only——它过去被确认过，这次也被召回，但 Agent 没说采用。反过来，Applied 也只是记录采用行为，不代表任务做对了。展示时 Memory Library 看历史积累，Run Memory 看本次到底给了谁什么、有没有采用。还要和跨题交接卡区分开：开发继承预约程序是产物交接，QA 召回验收做法是个人记忆。最后我们很谨慎：采用不等于性能提升，要证明有效还得做记忆开/关的对照实验。

**EN**

Our memory story fits in three sentences. One: memory belongs to an agent and a channel, stored in each team's SQLite. Whoever did the work keeps the experience — successes, failures and partial outcomes, all with run, task and call references. Private-channel experience never leaks into a group task. Two: one success only makes a candidate strategy. After a subtask is done, we group by task signature; the same signature succeeding twice on distinct subtasks, or a human confirming it, promotes it to verified. The screenshot shows a verified and a candidate side by side. Three: at the next task, recall is on demand — local lexical retrieval, no embedding API. Verified strategies come first, and candidates are clearly labelled unverified. Two dimensions are easy to mix up. One is the strategy's own status, candidate or verified — that's about how much evidence it has. The other is what happened this time: retrieved only, or explicitly applied through `memory.use`. A strategy can be verified and still retrieved-only: it was confirmed before, it showed up again, but the agent never said it used it. And applied doesn't mean correct — that's still judged by acceptance. In the demo, Memory Library shows the accumulation; Run Memory shows what was handed to whom this time. That's different from cross-task handoff cards: inheriting the booking app is artifact reuse, while QA recalling an acceptance recipe is personal memory. We're careful here too — adoption isn't proven improvement; that would need a controlled memory-on/off comparison.

---

## P7 · Mid-Test — Software Team, Task 1

**中文**

进入六个任务的实际记录。软件组第一题是会议室预约系统：用户抱怨共享表格导致重复预约、取消不更新，要求做一个能查空档、预约、取消的系统，并且要能演示怎么用。平台生成的分工是四步：技术 Lead 统筹，Software Developer 实现，Test Engineer 验证，Release Manager 发版说明，最后由 Test Engineer 做一次独立验收。截图上是 4/4 子任务完成、22 次模型请求、约 28.8 万 token、1 分 10 秒，平台验收 Pass，落盘五个产物——HTML、JS、CSS、README 和 release notes。这里想强调的是截图最后那行小字：执行完成和独立验收是分开记录的。Developer 发布文件之后，QA 针对的必须是这个已发布版本，而不是自己本地改过的副本；这个约定在后面两道软件题里会反复出现。

**EN**

Now the six actual task records. Software task one is a meeting-room booking system: the user is tired of a shared spreadsheet with double bookings and stale cancellations, and wants something that can show availability, book a room, cancel a booking, and be demoed. The plan has four steps — a technical lead to coordinate, a developer to build it, a test engineer to verify it, a release manager for the release notes — plus a separate independent acceptance by the test engineer. In the screenshot: four out of four subtasks done, 22 model requests, about 288k tokens, one minute ten seconds, platform acceptance pass, and five artifacts on disk — HTML, JS, CSS, README and release notes. The line I want you to notice is at the bottom: execution completion and independent acceptance are recorded separately. Once the developer publishes, QA has to test that published version, not a private copy it tweaked itself. That rule comes back in the other software cases.

---

## P8 · Mid-Test — Software Team, Task 2

**中文**

软件组第二题是在第一题的基础上加一个交互看板：按日期范围统计每个房间被预约多少次、占用多长时间、哪几天最忙、取消多少次，帮管理者决定要不要调整房间分配。分工同样是四步：发版说明 v1.1、独立验收、开发看板、验证看板。截图显示 4/4 完成、36 次模型请求、83.9 万 token、4 分 44 秒，平台验收 Pass。和第一题比，模型请求只从 22 涨到 36，token 却接近三倍，时间也翻了好几倍——这就是我们后面反思「重复测试和上下文反复加载吃掉预算」的直接来源。另一个重点是「用系统的预约记录」这句话：第二题不是从零重写，开发要拿到上一题发布的预约程序和数据。我们的做法是通过显式的只读授权继承快照，而不是让 Agent 自己去猜文件在哪。顺带说明一下口径：PPT 展示的是当时那次运行的通过记录；当前快照里 Node-only 的 JS 测试脚本还有一个已知的验收限制，冻结的六题测量并没有拿到六个全通过。

**EN**

Software task two extends the booking system with an interactive dashboard: for a date range, show how often each room was booked, total hours reserved, busiest days and times, and cancellations, so the manager can decide whether to reallocate rooms. The four subtasks mirror task one — release notes v1.1, independent acceptance, build the dashboard, verify it. The record shows four of four done, 36 model requests, about 839k tokens, four minutes forty-four, platform acceptance pass. Compare that with task one: requests went from 22 to 36, but tokens nearly tripled and time jumped even more. That gap is exactly where our later reflection on repeated testing and context reloads comes from. The other key phrase is "use the system's booking records." This is not a rewrite from scratch — the developer has to inherit the published app and data from the previous task. We do that with explicit read-only grants on snapshots, not by hoping the agent guesses where the files are. One caveat on framing: these are passing records from the run shown in the deck; in the current snapshot there's still a known acceptance limitation for Node-only JS tests, and the frozen six-case measurement is not six clean passes.

---

## P9 · Mid-Test — Newsroom, Task 1

**中文**

新闻组第一题要求自选一个近期港科大的 AI 事件，调查后做成面向学生和教职工的多渠道报道，并且要和用户讨论选题和受众需求。分工是 Editor-in-Chief 选题统筹、Reporter 调查、Editor 起草多渠道稿件、Fact-Checker 核实事实并批准发布，最后再由 Fact-Checker 独立验收产物。记录是 4/4 完成、28 次模型请求、约 30.3 万 token、1 分 43 秒，平台验收 Pass，产出五份文件：事件简报、报道、网页稿、编辑备注和事实核查报告。截图里那份核查报告值得点一下：它把报道里的每一条主张列成表，标注 PASS 或问题，并且把来源页面的 URL、HTTP 200 的抓取记录和快照哈希写在一起。也就是说，核查不是模型说「我觉得对」，而是主张、证据和文件版本三者绑定。当然这只是平台的验收证据，不等于课程层面的最终接受。

**EN**

Newsroom task one asks the team to pick a recent AI event at HKUST, investigate it and prepare a multi-channel report for students and faculty, with room to discuss the story choice and audience. The plan is editor-in-chief to coordinate, reporter to investigate, editor to draft the channel package, fact-checker to verify and approve publication, and then a separate independent acceptance by the fact-checker. The record: four of four done, 28 model requests, about 303k tokens, one minute forty-three, platform acceptance pass, five artifacts — the event brief, report, web copy, editorial notes and a fact-check report. The report is worth a closer look: it turns every claim in the article into a row with a verdict, and ties each source to a URL, an HTTP 200 fetch and a snapshot hash. So fact-checking isn't the model saying "sounds right" — it's claim, evidence and file version bound together. Again, that's platform evidence, not the course's final acceptance.

---

## P10 · Mid-Test — Newsroom, Task 2

**中文**

新闻组第二题是核查一句流行说法：「AI 写作检测器能可靠判断学生作业是不是 AI 写的」。要求查证据、写一篇给学生和教职工看的核查文章，说清结论和仍然不确定的地方。分工是 Reporter 找证据、Editor 写文章、Fact-Checker 独立核查文章主张并做最终验收。这里是 4/4 完成、30 次模型请求、约 45.7 万 token、2 分 01 秒，平台验收 Pass，一共八份产物，其中四份标着 "Carried over"——正好是上一题的简报、报道、网页稿和核查报告。这就是跨任务沿用的可视化证据：第二题没有重做第一题，而是在第一题的快照上追加了 AI 检测器证据、核查文章和文章核查报告。token 从 30.3 万涨到 45.7 万，也说明「沿用」不等于「零成本」：Planner 仍要读旧产物、要让核查者看当前版本。还有一个细节我们挺看重：文章明确写出证据不足和剩余不确定性，而不是硬给一个非黑即白的结论。

**EN**

Newsroom task two tests the claim that AI-writing detectors can reliably tell whether a student's assignment was written by AI. The team has to examine the evidence, write a fact-check article for students and faculty, and be explicit about both the conclusion and what remains uncertain. The roles: reporter gathers evidence, editor writes the piece, fact-checker independently verifies the article's claims and handles final acceptance. Four of four done, 30 model requests, about 457k tokens, two minutes one second, platform pass, eight artifacts — and four of them are marked "carried over," exactly the brief, report, web copy and fact-check report from task one. That's the visual proof of cross-task reuse: task two didn't redo task one, it added detector evidence, the article and the article review on top of the earlier snapshots. But tokens still rose from about 303k to 457k, so reuse isn't free — the planner still reads old artifacts and the reviewer still checks the current version. One detail I like: the article says plainly where the evidence is thin and what's still uncertain, instead of forcing a black-and-white verdict.

---

## P11 · Mid-Test — Fund Team, Task 1

**中文**

基金组第一题给了一个很具体的场景：25 岁、100 万港币、想投美股。要求研究并给出模拟投资方案、解释配置和风险，再写一段客户能看懂的普通话介绍。分工是 Investment Research Analyst 做市场研究和候选池，Fund Manager 出组合方案和配比理由，Marketing Specialist 写客户介绍，Risk Manager 做独立风险与一致性评审，最后也是 Risk Manager 独立验收。记录是 5/5 完成、25 次模型请求、约 32.2 万 token、2 分 05 秒，平台验收 Pass，三份产物：市场研究、组合方案、介绍文档。截图里的介绍文档有个细节很能说明我们怎么约束幻觉：开头就写明这是模拟、没有用实时价格、不构成投资建议，然后才给出 88% 股票、10% 海外、2% 现金类资产的配比表。这个免责声明不是用户要求的原话，但风险评审会检查这类边界——模拟任务和真实建议之间的界限，是我们特别在意的一条。

**EN**

Fund team task one is concrete: a 25-year-old with one million HKD wants to invest in US stocks. The team must research, build a simulated portfolio, explain the allocation and risks, and write an introduction the client can actually understand. The roles are investment research analyst for the market and candidate universe, fund manager for the proposal and allocation rationale, marketing specialist for the plain-language intro, and risk manager for the independent risk and consistency review — plus the final independent acceptance. Five of five done, 25 model requests, about 322k tokens, two minutes five, platform pass, three artifacts. The intro document has a detail that shows how we keep hallucination in check: it opens by saying this is a simulation, no live prices were used, and it's not investment advice — then it gives the allocation table, roughly 88% equities, 10% overseas and 2% cash-like. That disclaimer isn't verbatim from the prompt, but the risk review checks boundaries like this. Keeping a simulated exercise clearly separate from real advice is something we care about a lot.

---

## P12 · Mid-Test — Fund Team, Task 2

**中文**

基金组第二题是把视角放到 2026 年底，做一份半导体行业未来的分析展示，要求用浏览器能逐页播放的 HTML 幻灯片，自己决定研究范围、内容结构和呈现方式，投资含义和关键风险都要从研究里推出来。分工就是 P3 那张图：研究员做行业研究、基金经理写投资观点、营销专员做 HTML 幻灯片、风险经理审幻灯片和观点，最后独立验收。记录是 5/5 完成、29 次模型请求、约 51.5 万 token、4 分 16 秒，平台验收 Pass，六份产物——其中三份明确标着 "Carried over"，是第一题的市场研究、组合方案和介绍文档。新产出的是行业展望、投资观点 memo 和 HTML 幻灯片。这一题把前面几页串起来了：P3 的依赖图、P5 的澄清和审批、P4 的版本绑定与浏览器测试，都发生在这条链路里。它也是最依赖「接着已有产物做」的一题，同时也最贵，所以反思部分不是纸上谈兵。

**EN**

Fund task two takes an end-of-2026 view of the semiconductor industry and asks for an analytical deck delivered as HTML slides you can open and present page by page in a browser. The team decides the scope, structure and design itself, but the investment implications and key risks have to come out of the research. This is the exact graph we saw on slide three: analyst researches, fund manager writes the view, marketing specialist builds the deck, risk manager reviews both, then a separate acceptance. Five of five done, 29 model requests, about 515k tokens, four minutes sixteen, platform pass, six artifacts — three of them explicitly marked "carried over" from task one: the market research, portfolio proposal and intro. The new files are the industry outlook, the investment memo and the HTML deck. This one ties the whole talk together: the dependency graph from slide three, the clarification and approval from slide five, the hash binding and browser test from slide four all happened here. It's also the case that leans hardest on reusing earlier artifacts — and the most expensive one, which is why the reflection section isn't theoretical.

---

## P15 · Reflection — Repeated Testing Consumes the Budget

**中文**

第一条反思：反复测试吃掉了预算，测的还可能不是正确版本。早期 R13 里，开发 Agent 对同一份已经发布的源码发了 135 次浏览器调用；另一道看板题里，QA 做了 6 次浏览器检查，但测的是它自己改过的私有 `dashboard.js`，不是交付给用户的版本。新版还补了一个更常见的现象：有些明明很简单的任务，开发者已经把结果自查过了，QA 还会再走一轮重复检查。发现问题也没用，因为你没法据此判断用户拿到的那份有没有毛病。我们的改法有五步：用 SHA-256 把发布文件、测试记录和验收结论绑在一起；同一角色对同一版本做过的相同检查可以复用证据，但独立 QA 仍要亲自验证当前发布版本；优先覆盖用户明确要求的行为，而不是自己想出来的边角；返工单要写清预期结果、实际表现、受影响文件和证据；最后，CrewAI 的 guardrails 只检查任务输出、只在校验失败时触发重试，而不是无条件地再跑一轮。核心教训是：测试次数不等于测试质量，版本绑定加上有条件的重试，才算有效证据。

**EN**

First reflection: repeated testing burns the budget, and sometimes it isn't even testing the right version. In an early run, the developer agent made 135 browser calls against the same already-published source. In another dashboard case, QA ran six browser checks — but on its own private copy of `dashboard.js`, not on the version that was actually delivered. The updated slide adds a more everyday case too: even on simple tasks, QA would run another round after the developer had already verified the result. Even if it found a bug, you couldn't tell whether the user's copy had that bug. Our fix has five parts. Bind published files, test records and verdicts together with SHA-256. Let the same role reuse evidence for an identical check on an identical version, while independent QA still verifies the current published version itself. Prioritize the behaviour the user actually asked for, not edge cases we invented. Write rework tickets with expected versus actual behaviour, the affected files and the evidence. And use CrewAI guardrails to check task outputs and trigger retries only when validation fails — not just because another round might be nice. The lesson: the number of tests isn't the quality of tests; version binding plus conditional retries is what makes the evidence count.

---

## P16 · Reflection — Endless Refinement Before Delivery

**中文**

第二条反思：在交付之前无限打磨。接近截止时间时，Agent 还在重读源码、继续搜索、调整页面样式；有时候聊天里已经写出「完成了什么」，但可测试的文件根本没发布，下游 QA 手里没有有效版本，评审自然无从谈起。注意这里的关键词是「可测试的文件」——聊天里的完成宣言不算完成。我们的改法是把发布真实文件定义为生产步骤的完成条件；核心功能和基本检查过了就尽早 handoff；工具回执直接结束这一步，不让模型再复述一遍成果；同时给独立验收预留时间和预算——生产 250 万 token / 320 次请求，验收另外预留 75 万 / 80 次；只有评审确认了具体缺陷才启动返工。一句话：Publish a testable version early and reserve time for QA。早一点交出可测版本，比在截止前把页面再漂亮一点重要得多。

**EN**

Second reflection: endless polishing right before the deadline. Near the time limit, the agent is still rereading source, searching, tweaking styles. Sometimes the chat already says "here's what I completed," but no testable file was ever published, so downstream QA has nothing valid to review. The keyword is "testable file" — a completion claim in the chat doesn't count as completion. Our fix: publishing a real file is the completion condition for a producer step. Once core features and basic checks pass, hand off early; the tool receipt ends the step instead of making the model restate everything. And we reserve capacity for independent acceptance — 2.5 million tokens and 320 requests for production, with 750k tokens and 80 requests held back for review. Rework only starts when the reviewer confirms a specific defect. So the takeaway is: publish a testable version early and reserve time for QA. That matters far more than making the page a little prettier at the last minute.

---

## P17 · Reflection — Lost Context Across Tasks

**中文**

第三条反思：跨任务丢上下文。同一个任务里，旧源码、浏览器输出和返工说明被反复塞回上下文；换到下一题，Planner 又不知道上一题留下了哪些文件、哪些步骤走通过。软件组做使用量看板的时候，尤其需要知道第一题的预约程序和数据该怎么沿用。我们分两条线解决：团队层面，给下一题的 Planner 一张交接卡，写清产物路径、哈希、未解决问题和沿用规则，让它接着已有成果做；个人层面，经验按 Agent × 频道保存，按当前任务相关性召回，一次成功先记为候选，同一签名在不同子任务上重复成功才升级为已验证策略。这样，开发继承预约程序靠的是产物交接，QA 找回验收做法靠的是个人记忆与策略演进。要强调两者不能互相替代：文件不会因为 Agent「记得」就自动可读，经验也不会自动变成正确的产物。

**EN**

Third reflection: losing context across tasks. Within one task, old source, browser output and rework notes get stuffed back into context again and again. Move to the next task, and the planner has no idea which files the previous one left behind or which steps actually worked. For the software dashboard, the team really needed to know how to reuse the booking app and its data from task one. We split the fix into two tracks. At the team level, the next task's planner gets a handoff card with artifact paths, hashes, open issues and reuse rules, so it continues from existing work. At the individual level, experience is stored per agent and channel, recalled by relevance to the current task; one success is logged as a candidate, and only repeated success on distinct subtasks with the same signature promotes it to verified. So the developer inheriting the booking app is artifact handoff, while QA recalling an acceptance recipe is personal memory and strategy evolution. Neither replaces the other: a file isn't readable just because an agent "remembers" it, and a memory doesn't automatically become a correct artifact.

---

## P18 · Reflection — Extra Model Call After Handoff

**中文**

第四条反思：handoff 之后还要多付一次模型调用。原来的流程里，Agent 把文件发布成功之后，还会再调一次模型，让模型把已经写好的东西总结成一段 handoff；下游 Agent 必须等这段总结出来才能开工。但发布这一步明明已经拿到了真实回执——路径、哈希、测试结果都齐了——我们却还要花一次模型请求把它复述一遍，既多花 token 和时间，又把下游卡在队尾。新版的做法是一次调用把路径和摘要一起交掉：CrewAI 的 `result_as_answer=True` 允许工具结果直接作为 Task 的最终答案，发布回执本身就结束子任务，调度器立刻把下游需要的输入准备好，省掉那一轮纯总结的模型调用。注意这不等于跳过验收：独立 QA 仍然要针对发布版本做检查；我们省掉的只是「让模型再说一遍已经发生的事」。guardrails 依旧只在校验失败时重试。

**EN**

Fourth reflection: an extra model call after handoff. In the old flow, once the agent published a file, it called the model again to summarize what it had just done, and the next agent couldn't start until that summary came back. But publication already produced a real receipt — path, hash, test results. We were paying another model request just to restate it, in tokens and time, while downstream sat waiting. The update folds that into one handoff call: with `result_as_answer=True`, a tool result can serve as the task's final answer, so the publication receipt closes the subtask immediately and the scheduler prepares the downstream input with no extra summarization turn. That doesn't skip acceptance — independent QA still checks the published version; we only removed the "tell us again what just happened" step. Guardrails still retry only when validation actually fails.

---

## P19 · Reflection — Duplicate Output Generation

**中文**

第五条反思：同一份产物生成两遍。Agent 已经把完整的 HTML 幻灯片或报告写进文件了，但任务提示词又要求它在 final answer 里把同样的内容再输出一遍。结果就是模型在回答里重新生成一份长文，既拖慢输出、浪费 token，又让人分不清哪个才是最新版——文件里的和回答里的可能还不一致。现在的契约写得很清楚：完整交付物只放在文件里，`expected_output` 明确要求最终回复只给文件路径、验证证据和未解决问题。CrewAI 的 `expected_output` 在这里的作用，就是把「最终回答是什么格式」显式化：产品要求放进 `description`，回答只要一条短回执。这和 `delivery-v8` 的执行检查点是配套的——检查点只根据真实调用记录写过哪些路径、当前哈希、做过的测试和待处理的裁决，内容没变的重复发布直接复用版本和评审。最后要强调：这不是模型「不听话」，而是我们原来给的契约有歧义；把接口写清楚，比事后责怪模型有效得多。

**EN**

Fifth reflection: generating the same deliverable twice. The agent had already written the full HTML deck or report into a file, but the task prompt also asked it to reproduce that content in its final answer. So the model generated a second long text in the reply — slowing the output, burning tokens, and making it genuinely unclear which version was current, since the file and the reply could disagree. The contract is now explicit: the complete deliverable lives in the file, and `expected_output` requires the final reply to contain only the file path, verification evidence and any open issues. CrewAI's `expected_output` is what makes the response format unambiguous — product requirements go in `description`, and the answer is a short receipt. That lines up with the `delivery-v8` checkpoint, which is built only from actual calls: paths written, current hashes, tests run and pending verdicts. Republishing unchanged bytes just reuses the version and its review. One last point: this isn't the model being disobedient. Our original contract was ambiguous. Writing the interface clearly beats blaming the model afterwards.
