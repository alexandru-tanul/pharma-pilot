from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "app_chat" (
    "id" BIGSERIAL NOT NULL PRIMARY KEY,
    "title" VARCHAR(200) NOT NULL DEFAULT '',
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "session_key" VARCHAR(40)
);
COMMENT ON TABLE "app_chat" IS 'Chat model for storing chat history.';
CREATE TABLE IF NOT EXISTS "app_message" (
    "id" BIGSERIAL NOT NULL PRIMARY KEY,
    "role" VARCHAR(20) NOT NULL,
    "content" TEXT,
    "tool_calls" JSONB,
    "tool_call_id" VARCHAR(100),
    "name" VARCHAR(100),
    "usage" JSONB,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "chat_id" BIGINT NOT NULL REFERENCES "app_chat" ("id") ON DELETE CASCADE
);
COMMENT ON TABLE "app_message" IS 'Message model for individual chat messages.';
CREATE TABLE IF NOT EXISTS "aerich" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "version" VARCHAR(255) NOT NULL,
    "app" VARCHAR(100) NOT NULL,
    "content" JSONB NOT NULL
);"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        """


MODELS_STATE = (
    "eJztWW1T2zgQ/isaf6JzlIJJCNeZ++Cm0OaakA6kvU5LxyNsxdEgS6mlAJkO//20sh2/xS"
    "ahQKHDJ+LV7mr1rLR6Vvy0QuETJre6E6ys1+inxXFI9I+CfBNZeDrNpCBQ+IwZRT3ieqnW"
    "mVQR9sDTGDNJtMgn0ovoVFHBQRv8IeMFjUWEpBIR5QECB2hC4XO+BZ584WlXemhVo1N+yo"
    "+pN0G+CDHlifolVRMUEjURvjS2IeY4AOOQSIkDIhHmPqJckWiMPRgwJv3+ADkfeyaUGac/"
    "ZsRVItB+SKQD+vZdiyn3yRWR6ef03B1TwvwCiNQHB0buqvnUyN7QoMfVodGFhZ65nmCzkG"
    "f607mOly8MdHAgDQgnEVYEZlDRDLDlM8aSNKRwx8FmKnGUORufjPGMQYbAupKgVJiDPxF5"
    "gkNydTTSrDGAWV7+bdu7ux17e3dvv93qdNr72/ta14RUHepcxwvOAIldGVh673pHI1io0D"
    "so3loguDY2WOHYyuCdAayo0qoVjPWGiZYjvDAogayXVgY5hbQJ5VSQwZzt+xRny/oFlEN8"
    "5TLCAzXRn/b2dgOCn53j7nvneENrvSjieJQM2fEYQJpB6EUEFuzGJ7iI41s9omhIlmNZtC"
    "wB6iemW+mP+4L3F7exXoM/5GyenJAGfEe9wcHJyBl8hJWEUv5gBiJndAAjtpHOS9KNvVIq"
    "Fk7Qf73RewSf6Ovw6MAgKKQKIjNjpjf6akFMeKaEy8Wli/3cYU6lKTCFxM6m/i0TW7R8Tu"
    "xvTWwSfJZXqW8uDYZ7TubrVL6S2a3qXxLdQ6axUABbq9S/Vn35a5nqB9f1+Dx3n4DgDHvn"
    "lzjy3cJIjg8lfGHJhZ5YHn44JgybBVbxTejUIPbyOI/MdbplUmm6+wAfYYs6xKpDoR2WJc"
    "C7TNQwN8xUQmQJ98yB1Uw/w5zijQw08ZrjkxpRekH9GWYxpUxTXSWia9gCHz0UjIlLiYZT"
    "wp0eMjS2K8IpIxCMBIYJTkItnknio7M56lNFNPl8fcpfok+SRK+wPrVSYZ65RhN8QVAkGE"
    "F/IZ0sRbgCdaeq6WEeayshmOthxjTfjSI8B/2RliFdlaY6FLLE+z+nFpidWnqahb1L/WzW"
    "Z3b8uNkxZHGdKyLVfzhufJeXg70aOW7gxhVqnG3zIoQjclWzR3MmT+SGbSJGB19GBU6Ugr"
    "UxcL68KPCi/vDoXaqeA7fbH74pgZqVoiqu/54Mj2r6toJVCdpPXK/5m089tYmYroHfnxrQ"
    "sO5moMuYllglOKgF2l1WaBta5JLdE9nHxWKws1KrvNPQKu9UW2Xzdw0kU/1nBBc9acrTVj"
    "33C4PnI7/CkX9+y/kjWv7qWw70FkvLeBNfzhndTJofSQofjDdXHgGKYFeRPhQRoQH/QOYG"
    "7R6HXstbVptK/zp5fCjXNfpaHOHLRaeW30B6eXpRRMU3nnPSdd4eWNf1Dyf3+WTgkIh6E2"
    "vJi0Eystn4YJDp3PRWUA/oHXe9f1DLa++0Oq393b3W4sQuJE0H9eZm9oJEMnlbW5V/5Uye"
    "aEvbbq/S07bb9U0tjBVJAhyNNUBM1J8mgPdCYmufBeppbP2zwG2I7O+7Mu6Vya7xLn/318"
    "v1/4V6mTA="
)
