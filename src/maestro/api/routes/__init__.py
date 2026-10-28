from maestro.api.routes import (
    admin,
    attachments,
    batches,
    catalog,
    collections,
    configuration,
    identity,
    plans,
    runs,
    system,
)


def routers():
    return [
        system.router,
        identity.router,
        admin.router,
        catalog.router,
        configuration.router,
        attachments.router,
        plans.router,
        runs.router,
        batches.router,
        collections.router,
    ]
