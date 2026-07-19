from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class UnitOfWork:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory
        self.session: AsyncSession | None = None

    async def __aenter__(self) -> "UnitOfWork":
        self.session = self.session_factory()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, _: object
    ) -> bool:
        if self.session is None:
            return False
        try:
            if exc_type is not None:
                await self.session.rollback()
            else:
                await self.session.commit()
            return False
        finally:
            await self.session.close()
