"""Astra's observed chat-completions endpoint, with bounded SSE and explicit completion."""
import codecs
import json
import re
import time
import httpx
from . import connectors as c


class IncompleteAnswer(c.ConnectorError):
    def __init__(self, message, partial=''):
        super().__init__(message)
        self.partial=c.redact_sensitive_text(partial)


def chat(settings, messages, progress=None):
    # A value copied from DevTools often includes the header scheme. Normalize
    # only this adapter, including already saved values, without logging it.
    settings=dict(settings)
    settings.pop('token',None)  # Logs credentials never replace the AI key.
    key=settings.get('api_key','')
    if not isinstance(key,str):raise c.ConnectorError('Неверный формат токена Astra AI')
    key=re.sub(r'^Bearer\s+','',key.strip(),flags=re.I)
    if not key:raise c.ConnectorError('Нужен отдельный Bearer token Astra AI')
    if any(ch.isspace() for ch in key):raise c.ConnectorError('В поле Astra AI нужен один Bearer token без заголовков и переносов строк')
    settings['api_key']=key
    url=c._url(settings.get('url'))
    model=c._text(settings.get('model'),'model',200)
    if not model:raise c.ConnectorError('Нужен model ID Astra AI')
    if not isinstance(messages,list) or not messages or len(messages)>60:raise c.ConnectorError('Неверный формат контекста Astra AI')
    clean=[]
    for message in messages:
        if not isinstance(message,dict) or message.get('role') not in ('system','user','assistant'):raise c.ConnectorError('Неподдерживаемая роль чата')
        clean.append({'role':message['role'],'content':c._text(message.get('content'),'message',100000)})
    if not clean or len(clean)>60 or sum(len(m['content']) for m in clean)>150000:
        raise c.ConnectorError('Контекст Astra AI превышает лимит')
    parts=[];done=False;finished=False;data=[];event='message';size=0;buffer='';count=0;published=0
    deadline=time.monotonic()+180
    decoder=codecs.getincrementaldecoder('utf-8')('strict')
    def publish(force=False):
        nonlocal published
        if progress and (force or time.monotonic()-published>=1):
            progress(c.redact_sensitive_text(''.join(parts)));published=time.monotonic()
    def dispatch():
        nonlocal done,finished,data,event,count
        payload='\n'.join(data);data=[];kind=event;event='message'
        if not payload:return
        count+=1
        if count>10000:raise c.ConnectorError('Слишком много событий Astra AI')
        if kind=='error':raise c.ConnectorError('Astra AI сообщила ошибку в потоке')
        if payload.strip()=='[DONE]':done=True;return
        try:item=json.loads(payload)
        except ValueError:raise c.ConnectorError('Astra AI вернула некорректное SSE-событие') from None
        if not isinstance(item,dict) or item.get('error'):raise c.ConnectorError('Astra AI вернула ошибку вместо ответа')
        choices=item.get('choices')
        if choices==[] and item.get('usage') is not None:return
        if not isinstance(choices,list) or len(choices)!=1 or not isinstance(choices[0],dict):
            raise c.ConnectorError('Неподдерживаемая структура choices Astra AI')
        choice=choices[0];delta=choice.get('delta',{})
        if choice.get('index',0)!=0:raise c.ConnectorError('Неподдерживаемый индекс ответа Astra AI')
        if not isinstance(delta,dict):raise c.ConnectorError('Неподдерживаемый delta Astra AI')
        if delta.get('tool_calls') or delta.get('function_call'):
            raise c.ConnectorError('Astra AI запросила выполнение инструмента; этот адаптер не исполняет команды')
        content=delta.get('content')
        if content is not None:
            if not isinstance(content,str):raise c.ConnectorError('Текст Astra AI имеет неподдерживаемый формат')
            if finished and content:raise c.ConnectorError('Текст получен после завершения ответа')
            parts.append(content)
            if sum(map(len,parts))>100000:raise c.ConnectorError('Ответ Astra AI превышает лимит')
            publish()
        reason=choice.get('finish_reason')
        if reason is not None:
            if reason!='stop':raise c.ConnectorError('Astra AI не завершила ответ нормально: '+str(reason)[:30])
            finished=True
    def line(value):
        nonlocal event
        if not value:dispatch();return
        if value.startswith(':'):return
        field,sep,value=value.partition(':')
        if value.startswith(' '):value=value[1:]
        if field=='data':data.append(value)
        elif field=='event':event=value
    try:
        with c._client(settings,timeout=httpx.Timeout(45,connect=10)) as client:
            with client.stream('POST',url,json={'model':model,'stream':True,'messages':clean},headers={'Accept':'text/event-stream'}) as response:
                if response.status_code!=200:
                    hint={401:'Astra отклонила авторизацию. Обновите Bearer token именно Astra AI в настройках кластера и проверьте поток. Токен логов Astra и токен Kubernetes — отдельные подключения; причина 401 может быть в истёкшем или неподходящем токене.',
                          403:'Astra запретила доступ к AI endpoint. Проверьте права учётной записи в Astra.'}.get(response.status_code,'Проверьте выбранный AI endpoint.')
                    raise c.ConnectorError(f'Astra AI HTTP {response.status_code}. '+hint)
                if response.headers.get('content-type','').split(';',1)[0].strip().lower()!='text/event-stream':
                    raise c.ConnectorError('Astra AI не вернула text/event-stream; проверьте URL и авторизацию')
                for chunk in response.iter_bytes():
                    if time.monotonic()>deadline:raise c.ConnectorError('Превышен бюджет времени Astra AI')
                    size+=len(chunk)
                    if size>c.MAX_RESPONSE_BYTES:raise c.ConnectorError('Поток Astra AI превышает 16 MiB')
                    buffer+=decoder.decode(chunk)
                    while (match:=re.search(r'\r\n|[\r\n]',buffer)) is not None:
                        if match.group()=='\r' and match.end()==len(buffer):break
                        value=buffer[:match.start()];buffer=buffer[match.end():];line(value)
                        if done:break
                    if len(buffer)>262144 or sum(map(len,data))>262144:raise c.ConnectorError('SSE-событие слишком большое')
                    if done:break
                if not done:
                    buffer+=decoder.decode(b'',final=True)
                    if buffer:line(buffer.rstrip('\r'))
                    if data:dispatch()
        if not finished:raise c.ConnectorError('Поток завершился без finish_reason=stop; полнота ответа не подтверждена')
        answer=''.join(parts)
        if not answer.strip():raise c.ConnectorError('Astra AI не вернула текст ответа')
        publish(True)
        return c.redact_sensitive_text(answer)
    except (httpx.HTTPError,UnicodeError,c.ConnectorError) as exc:
        message=str(exc) if isinstance(exc,c.ConnectorError) else 'Обрыв или ошибка сети/TLS при чтении Astra AI'
        publish(True)
        raise IncompleteAnswer(message+'; ответ не считается завершённым',''.join(parts)) from None
