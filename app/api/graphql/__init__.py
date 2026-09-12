import strawberry
from fastapi import Request, WebSocket
from strawberry.schema.config import StrawberryConfig
from strawberry.fastapi import GraphQLRouter

from app.api.graphql.helpers import get_context
from app.api.graphql.queries import Query
from app.api.graphql.mutations import Mutation
from app.api.graphql.subscriptions import Subscription
from app.core.i18n import (
    get_locale,
    locale_from_accept_language,
    locale_from_connection_params,
    reset_locale,
    set_locale,
)


async def get_localized_context(
    request: Request = None,
    websocket: WebSocket = None,
):
    headers = request.headers if request else (websocket.headers if websocket else {})
    locale = locale_from_accept_language(headers.get("accept-language"))
    if websocket:
        connection_locale = locale_from_connection_params(
            websocket.scope.get("connection_params")
        )
        if connection_locale:
            locale = connection_locale
    locale_token = set_locale(locale)
    try:
        async for context in get_context(request=request, websocket=websocket):
            context["locale"] = get_locale()
            yield context
    finally:
        reset_locale(locale_token)


schema = strawberry.Schema(
    query=Query,
    mutation=Mutation,
    subscription=Subscription,
    config=StrawberryConfig(
        auto_camel_case=True
    )
)

graphql_app = GraphQLRouter(schema, context_getter=get_localized_context)
