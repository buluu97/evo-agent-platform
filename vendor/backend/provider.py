"""HKGAI / DeepSeek Chat Completions transport; no OpenAI service or SDK."""
import json
import http.client
import ssl
import threading
import time
import urllib.error
import urllib.request
import zlib

_MAX_RESPONSE_BYTES=2_000_000


class ProviderError(RuntimeError):
    def __init__(self, message, code='provider', retryable=False, *, phase=None, transport=None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.phase = phase
        self.transport = dict(transport) if isinstance(transport,dict) else None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a bearer credential to a different URL via redirects.
        return None


class _CompatibleHTTPSConnection(http.client.HTTPSConnection):
    """Per-connection TLS write pacing; never changes routing or TLS settings."""
    def send(self,data):
        if self.sock is None:
            if self.auto_open:self.connect()
            else:raise http.client.NotConnected()
        # CONNECT tunnel setup uses an ordinary socket before TLS wrapping.
        # Leave it, and non-byte inputs, entirely to the standard implementation.
        if not isinstance(self.sock,ssl.SSLSocket) or not isinstance(data,(bytes,bytearray,memoryview)):
            return super().send(data)
        for offset in range(0,len(data),512):
            if getattr(self,'_compatible_has_written',False):time.sleep(.03)
            super().send(data[offset:offset+512])
            self._compatible_has_written=True


class _CompatibleHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self,request):
        # Inherit urllib's default verified SSL context, proxy CONNECT handling,
        # SNI and hostname checks; only the connection's send method differs.
        return self.do_open(_CompatibleHTTPSConnection,request,context=self._context)


class ModelClient:
    def __init__(self, config):
        self.config = config
        self._slots = threading.BoundedSemaphore(3)

    def complete(self, messages, max_tokens=None, deadline=None):
        if deadline is not None:
            from .provider_deadline import complete_before_deadline
            return complete_before_deadline(self,messages,max_tokens,deadline,json_protocol=False)
        return self._complete(messages,max_tokens)

    def complete_json(self, messages, max_tokens=None, deadline=None):
        """Request configured JSON formatting; callers still validate the actual output."""
        if deadline is not None:
            from .provider_deadline import complete_before_deadline
            return complete_before_deadline(self,messages,max_tokens,deadline,json_protocol=True)
        return self._complete(messages,max_tokens,json_protocol=True)

    def _complete(self, messages, max_tokens=None, *, json_protocol=False):
        started=time.monotonic()
        phase='prepare_request'
        mode=getattr(self.config,'transport_mode','standard')
        thinking_mode=getattr(self.config,'thinking_mode','default')
        json_mode=getattr(self.config,'json_mode','default') if json_protocol else 'default'
        response_format={'type':'json_object'} if json_mode=='json_object' else None
        details={'transportMode':mode,'requestedThinkingMode':thinking_mode,
                 'requestedResponseFormat':response_format,
                 'headersReceived':False,'receivedBytes':0,'expectedBytes':None,
                 'wireComplete':False,'decodedBytes':0,'bodyComplete':False,'requestEncoding':'identity'}

        def failure(message,code='provider',retryable=False):
            diagnostic={**details,'elapsedMs':round((time.monotonic()-started)*1000,3)}
            return ProviderError(self.config.scrub(message),code,retryable,
                                 phase=phase,transport=self.config.scrub(diagnostic))

        if not self.config.api_key:
            raise failure('未配置 LLM_API_KEY。请在本机 .env 中填写课程 API key。')
        if mode not in ('standard','compatible'):
            raise failure('模型传输模式无效。','parameters')
        if thinking_mode not in ('default','disabled'):
            raise failure('模型思考模式无效。','parameters')
        if thinking_mode=='disabled' and self.config.provider!='hkgai':
            raise failure('禁用思考的模板参数目前仅在 HKGAI 验证，未向其他 provider 发送。','parameters')
        if json_mode not in ('default','json_object'):
            raise failure('JSON 输出模式无效。','parameters')
        if json_mode=='json_object' and self.config.provider!='hkgai':
            raise failure('JSON 对象请求目前仅在 HKGAI 验证，未向其他 provider 发送。','parameters')
        requested = self.config.max_completion_tokens if max_tokens is None else max_tokens
        if isinstance(requested,bool) or not isinstance(requested,int) or requested<1:
            raise failure('模型输出预算必须为正整数。','parameters')
        requested=min(requested,self.config.max_completion_tokens)
        body = {'model': self.config.model, 'messages': messages, 'temperature': 0.2,
                'max_tokens': requested, 'stream': False}
        if thinking_mode=='disabled':
            body['chat_template_kwargs']={'enable_thinking':False}
        if response_format is not None:
            body['response_format']=response_format
        try:
            payload=json.dumps(body,ensure_ascii=False).encode('utf-8')
            request=urllib.request.Request(self.config.base_url+'/chat/completions',data=payload,
                headers={'Content-Type':'application/json','Authorization':'Bearer '+self.config.api_key},method='POST')
            if mode=='compatible':
                if request.type!='https':raise failure('兼容传输模式仅支持 HTTPS。','parameters')
                request.add_header('Accept-Encoding','gzip')
                details.update(configuredWriteChunkBytes=512,configuredWriteDelayMs=30)
        except (ValueError,TypeError):
            raise failure('模型请求无法编码为 UTF-8 JSON。','parameters') from None
        details['requestBytes']=len(payload)
        acquired=False
        try:
            phase='wait_slot'
            slot_started=time.monotonic()
            acquired=self._slots.acquire(timeout=self.config.model_timeout)
            details['slotWaitMs']=round((time.monotonic()-slot_started)*1000,3)
            if not acquired:
                raise failure('等待模型连接槽超时；尚未向上游发出请求。','provider_busy',True)
            # This bounds slot waiting and socket operations, not blocking DNS
            # resolution or a strict total wall-clock deadline.
            phase='wait_headers'
            headers_started=time.monotonic()
            handlers=[NoRedirect()]
            if mode=='compatible':handlers.append(_CompatibleHTTPSHandler())
            with urllib.request.build_opener(*handlers).open(request,timeout=self.config.model_timeout) as response:
                details.update(headersReceived=True,headersWaitMs=round((time.monotonic()-headers_started)*1000,3))
                status=getattr(response,'status',None)
                if isinstance(status,int):details['httpStatus']=status
                headers=getattr(response,'headers',None)
                declared=headers.get('Content-Length') if headers is not None and callable(getattr(headers,'get',None)) else None
                if headers is not None and callable(getattr(headers,'get',None)):
                    for header,key,known in (('Content-Encoding','contentEncoding',{'identity','gzip','deflate'}),
                                             ('Transfer-Encoding','transferEncoding',{'identity','chunked'})):
                        value=headers.get(header)
                        if isinstance(value,str):
                            normalized=value.strip().lower() or 'identity'
                            details[key]=normalized if normalized in known else 'other'
                if isinstance(declared,str):
                    try:
                        expected=int(declared)
                        if expected>=0:details['expectedBytes']=expected
                        else:details['invalidContentLength']=True
                    except ValueError:
                        details['invalidContentLength']=True
                phase='read_body'
                if details['expectedBytes'] is not None and details['expectedBytes']>_MAX_RESPONSE_BYTES:
                    raise failure('模型响应超过大小限制。','response_too_large')
                raw=bytearray()
                reader=getattr(response,'read1',None)
                if callable(reader):
                    while len(raw)<=_MAX_RESPONSE_BYTES:
                        if details['expectedBytes'] is not None and len(raw)>=details['expectedBytes']:break
                        block=reader(min(65536,_MAX_RESPONSE_BYTES+1-len(raw)))
                        if not block:break
                        raw.extend(block)
                        details['receivedBytes']=len(raw)
                else:
                    # Compatibility with small response fixtures; actual urllib
                    # HTTPResponse supports read1 for observable partial reads.
                    raw.extend(response.read(_MAX_RESPONSE_BYTES+1))
                    details['receivedBytes']=len(raw)
                if len(raw)>_MAX_RESPONSE_BYTES:
                    raise failure('模型响应超过大小限制。','response_too_large')
                if details['expectedBytes'] is not None and len(raw)!=details['expectedBytes']:
                    raise failure('模型 HTTP 正文不完整；未将部分字节作为完整 JSON。','transport_incomplete',True)
                details['wireComplete']=True
            phase='decompress_body'
            encoding=details.get('contentEncoding','identity')
            if encoding=='gzip':
                decoder=zlib.decompressobj(16+zlib.MAX_WBITS)
                try:
                    decoded=decoder.decompress(raw,_MAX_RESPONSE_BYTES+1)
                except zlib.error:
                    raise failure('模型 gzip 响应校验失败，未采用损坏或异常正文。','response_decompression',True) from None
                details['decodedBytes']=len(decoded)
                if len(decoded)>_MAX_RESPONSE_BYTES:
                    raise failure('模型 gzip 解压后响应超过大小限制。','response_too_large')
                if not decoder.eof:
                    raise failure('模型 gzip 响应在完整结束前中断；未采用部分解压正文。','transport_incomplete',True)
                if decoder.unconsumed_tail or decoder.unused_data:
                    raise failure('模型 gzip 响应包含额外或未消费数据，拒绝将其作为完整 JSON。','response_decompression')
                raw=decoded
            elif encoding!='identity':
                raise failure('模型响应使用未支持的 Content-Encoding。','response_encoding')
            else:
                details['decodedBytes']=len(raw)
            details['bodyComplete']=True
            phase='decode_json'
            try:
                result=json.loads(raw)
            except (ValueError,UnicodeError):
                raise failure('模型 HTTP 正文已接收，但不是有效的 JSON 响应。','response_json') from None
            phase='validate_response'
            if not isinstance(result, dict) or not isinstance(result.get('choices'), list):
                raise failure('模型 API 返回了无法识别的响应格式。','response_schema')
            choice = result['choices'][0]
            message = choice['message']
            if not isinstance(message, dict):
                raise failure('模型 API 返回了无法识别的响应格式。','response_schema')
            content = message.get('content')
            reason=choice.get('finish_reason')
            if reason=='length' and content is None:
                content=''  # Reasoning can consume the cap before visible JSON.
            if not isinstance(content, str) or (not content.strip() and reason!='length'):
                raise failure('模型没有返回有效的文本响应。','response_schema')
            return {'content': self.config.scrub(content), 'usage': self.config.scrub(result.get('usage', {})),
                    'model': result.get('model', self.config.model), 'finish_reason':reason,
                    'transport':self.config.scrub({**details,'elapsedMs':round((time.monotonic()-started)*1000,3)})}
        except urllib.error.HTTPError as error:
            # Do not echo provider response bodies; they may contain credentials or request content.
            details.update(headersReceived=True,httpStatus=error.code)
            raise failure(f'模型 API 返回 HTTP {error.code}；请检查课程密钥、额度、模型和地址。',
                                'authorization' if error.code in (401,403) else 'timeout' if error.code in (408,504) else 'provider_http',
                                error.code in (408,429,500,502,503,504)) from None
        except http.client.IncompleteRead as error:
            partial=error.partial if isinstance(error.partial,(bytes,bytearray)) else b''
            details.update(receivedBytes=details['receivedBytes']+len(partial),bodyComplete=False,exceptionType='IncompleteRead')
            raise failure('模型 HTTP 正文在结束前中断；未将部分字节作为完整 JSON。','transport_incomplete',True) from None
        except http.client.HTTPException as error:
            details['exceptionType']=type(error).__name__
            raise failure('模型 HTTP 传输协议失败；尚未获得可验证的完整响应。','transport',True) from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            reason=getattr(error,'reason',error)
            timed_out=isinstance(reason,TimeoutError) or 'timed out' in str(reason).lower()
            details['exceptionType']=type(reason).__name__
            if isinstance(getattr(reason,'errno',None),int):details['errno']=reason.errno
            label='模型 API 等待响应超时：' if timed_out else '模型 API 网络连接失败：'
            raise failure(label+type(reason).__name__, 'timeout' if timed_out else 'network', True) from None
        except (ValueError, KeyError, IndexError, TypeError):
            raise failure('模型 API 返回了无法识别的响应格式。','response_schema') from None
        finally:
            if acquired:self._slots.release()
