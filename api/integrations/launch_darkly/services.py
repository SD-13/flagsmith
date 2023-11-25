from contextlib import contextmanager
from typing import TYPE_CHECKING, Callable, Tuple

from django.core import signing
from django.utils import timezone
from requests.exceptions import RequestException

from environments.models import Environment
from features.feature_types import MULTIVARIATE, STANDARD, FeatureType
from features.models import Feature, FeatureState, FeatureStateValue
from features.multivariate.models import (
    MultivariateFeatureOption,
    MultivariateFeatureStateValue,
)
from features.value_types import STRING
from integrations.launch_darkly import types as ld_types
from integrations.launch_darkly.client import LaunchDarklyClient
from integrations.launch_darkly.constants import (
    LAUNCH_DARKLY_IMPORTED_DEFAULT_TAG_LABEL,
    LAUNCH_DARKLY_IMPORTED_TAG_COLOR,
)
from integrations.launch_darkly.models import (
    LaunchDarklyImportRequest,
    LaunchDarklyImportStatus,
)
from projects.tags.models import Tag

if TYPE_CHECKING:  # pragma: no cover
    from projects.models import Project
    from users.models import FFAdminUser


def _sign_ld_value(value: str, user_id: int) -> str:
    return signing.dumps(value, salt=f"ld_import_{user_id}")


def _unsign_ld_value(value: str, user_id: int) -> str:
    return signing.loads(
        value,
        salt=f"ld_import_{user_id}",
    )


def _log_error(
    import_request: LaunchDarklyImportRequest,
    error_message: str,
) -> None:
    import_request.status["error_message"] = error_message


@contextmanager
def _complete_import_request(
    import_request: LaunchDarklyImportRequest,
) -> None:
    """
    Wrap code so the import request always ends up completed.

    If no exception raised, assume successful import.

    In case wrapped code needs to expose an error to the user, it should populate
    `import_request.status["error_message"]` before raising an exception.
    """
    try:
        yield
    except Exception as exc:
        import_request.status["result"] = "failure"
        raise exc
    else:
        import_request.status["result"] = "success"
    finally:
        import_request.ld_token = ""
        import_request.completed_at = timezone.now()
        import_request.save()


def _create_environments_from_ld(
    ld_environments: list[ld_types.Environment],
    project_id: int,
) -> dict[str, Environment]:
    environments_by_ld_environment_key: dict[str, Environment] = {}

    for ld_environment in ld_environments:
        (
            environments_by_ld_environment_key[ld_environment["key"]],
            _,
        ) = Environment.objects.get_or_create(
            name=ld_environment["name"],
            project_id=project_id,
        )

    return environments_by_ld_environment_key


def _create_tags_from_ld(
    ld_tags: list[str],
    project_id: int,
) -> dict[str, Tag]:
    tags_by_ld_tag = {}

    for ld_tag in (*ld_tags, LAUNCH_DARKLY_IMPORTED_DEFAULT_TAG_LABEL):
        tags_by_ld_tag[ld_tag], _ = Tag.objects.update_or_create(
            label=ld_tag,
            project_id=project_id,
            defaults={
                "color": LAUNCH_DARKLY_IMPORTED_TAG_COLOR,
            },
        )

    return tags_by_ld_tag


def _create_boolean_feature_states(
    ld_flag: ld_types.FeatureFlag,
    feature: Feature,
    environments_by_ld_environment_key: dict[str, Environment],
) -> None:
    for ld_environment_key, environment in environments_by_ld_environment_key.items():
        ld_flag_config = ld_flag["environments"][ld_environment_key]
        feature_state, _ = FeatureState.objects.update_or_create(
            feature=feature,
            environment=environment,
            defaults={"enabled": ld_flag_config["on"]},
        )

        FeatureStateValue.objects.update_or_create(
            feature_state=feature_state,
        )


def _create_string_feature_states(
    ld_flag: ld_types.FeatureFlag,
    feature: Feature,
    environments_by_ld_environment_key: dict[str, Environment],
) -> None:
    variations_by_idx = {
        str(idx): variation for idx, variation in enumerate(ld_flag["variations"])
    }

    for ld_environment_key, environment in environments_by_ld_environment_key.items():
        ld_flag_config = ld_flag["environments"][ld_environment_key]

        is_flag_on = ld_flag_config["on"]
        if is_flag_on:
            variation_config_key = "isFallthrough"
        else:
            variation_config_key = "isOff"

        string_value = ""

        if ld_flag_config_summary := ld_flag_config.get("_summary"):
            enabled_variations = ld_flag_config_summary.get("variations") or {}
            for idx, variation_config in enabled_variations.items():
                if variation_config.get(variation_config_key):
                    string_value = variations_by_idx[idx]["value"]
                    break

        feature_state, _ = FeatureState.objects.update_or_create(
            feature=feature,
            environment=environment,
            defaults={"enabled": is_flag_on},
        )
        FeatureStateValue.objects.update_or_create(
            feature_state=feature_state,
            defaults={"type": STRING, "string_value": string_value},
        )


def _create_mv_feature_states(
    ld_flag: ld_types.FeatureFlag,
    feature: Feature,
    environments_by_ld_environment_key: dict[str, Environment],
) -> None:
    variations = ld_flag["variations"]
    variation_values_by_idx: dict[str, str] = {}
    mv_feature_options_by_variation: dict[str, MultivariateFeatureOption] = {}

    for idx, variation in enumerate(variations):
        variation_idx = str(idx)
        variation_value = variation["value"]
        variation_values_by_idx[variation_idx] = variation_value
        (
            mv_feature_options_by_variation[str(idx)],
            _,
        ) = MultivariateFeatureOption.objects.update_or_create(
            feature=feature,
            string_value=variation_value,
            defaults={"default_percentage_allocation": 0, "type": STRING},
        )

    for ld_environment_key, environment in environments_by_ld_environment_key.items():
        ld_flag_config = ld_flag["environments"][ld_environment_key]

        is_flag_on = ld_flag_config["on"]

        feature_state, _ = FeatureState.objects.update_or_create(
            feature=feature,
            environment=environment,
            defaults={"enabled": is_flag_on},
        )

        cumulative_rollout = rollout_baseline = 0

        if ld_flag_config_summary := ld_flag_config.get("_summary"):
            enabled_variations = ld_flag_config_summary.get("variations") or {}
            for variation_idx, variation_config in enabled_variations.items():
                if variation_config.get("isOff"):
                    # Set LD's off value as the control value.
                    # We expect only one off variation.
                    FeatureStateValue.objects.update_or_create(
                        feature_state=feature_state,
                        defaults={
                            "type": STRING,
                            "string_value": variation_values_by_idx[variation_idx],
                        },
                    )

                mv_feature_option = mv_feature_options_by_variation[variation_idx]
                percentage_allocation = 0

                if variation_config.get("isFallthrough"):
                    # We expect only one fallthrough variation.
                    percentage_allocation = 100

                elif rollout := variation_config.get("rollout"):
                    # 50% allocation is recorded as 50000 in LD.
                    # It's possible to allocate e.g. 50.999, resulting
                    # in rollout == 50999.
                    # Round the values nicely by keeping the `cumulative_rollout` tally.
                    cumulative_rollout += rollout / 1000
                    cumulative_rollout_rounded = round(cumulative_rollout)
                    percentage_allocation = (
                        cumulative_rollout_rounded - rollout_baseline
                    )
                    rollout_baseline = cumulative_rollout_rounded

                MultivariateFeatureStateValue.objects.update_or_create(
                    feature_state=feature_state,
                    multivariate_feature_option=mv_feature_option,
                    defaults={"percentage_allocation": percentage_allocation},
                )


def _get_feature_type_and_feature_state_factory(
    ld_flag: ld_types.FeatureFlag,
) -> Tuple[
    FeatureType,
    Callable[[ld_types.FeatureFlag, Feature, dict[str, Environment]], None],
]:
    match ld_flag["kind"]:
        case "multivariate" if len(ld_flag["variations"]) > 2:
            feature_type = MULTIVARIATE
            feature_state_factory = _create_mv_feature_states
        case "multivariate":
            feature_type = STANDARD
            feature_state_factory = _create_string_feature_states
        case _:  # assume boolean
            feature_type = STANDARD
            feature_state_factory = _create_boolean_feature_states

    return feature_type, feature_state_factory


def _create_feature_from_ld(
    ld_flag: ld_types.FeatureFlag,
    environments_by_ld_environment_key: dict[str, Environment],
    tags_by_ld_tag: dict[str, Tag],
    project_id: int,
) -> Feature:
    (
        feature_type,
        feature_state_factory,
    ) = _get_feature_type_and_feature_state_factory(
        ld_flag,
    )

    tags = [
        tags_by_ld_tag[LAUNCH_DARKLY_IMPORTED_DEFAULT_TAG_LABEL],
        *(tags_by_ld_tag[ld_tag] for ld_tag in ld_flag["tags"]),
    ]

    feature, _ = Feature.objects.update_or_create(
        project_id=project_id,
        name=ld_flag["key"],
        defaults={
            "description": ld_flag.get("description"),
            "default_enabled": False,
            "type": feature_type,
            "is_archived": ld_flag["archived"],
        },
    )
    feature.tags.set(tags)

    feature_state_factory(
        ld_flag=ld_flag,
        feature=feature,
        environments_by_ld_environment_key=environments_by_ld_environment_key,
    )

    return feature


def _create_features_from_ld(
    ld_flags: list[ld_types.FeatureFlag],
    environments_by_ld_environment_key: dict[str, Environment],
    tags_by_ld_tag: dict[str, Tag],
    project_id: int,
) -> list[Feature]:
    return [
        _create_feature_from_ld(
            ld_flag=ld_flag,
            environments_by_ld_environment_key=environments_by_ld_environment_key,
            tags_by_ld_tag=tags_by_ld_tag,
            project_id=project_id,
        )
        for ld_flag in ld_flags
    ]


def create_import_request(
    project: "Project",
    user: "FFAdminUser",
    ld_project_key: str,
    ld_token: str,
) -> LaunchDarklyImportRequest:
    ld_client = LaunchDarklyClient(ld_token)

    ld_project = ld_client.get_project(project_key=ld_project_key)
    requested_flag_count = ld_client.get_flag_count(project_key=ld_project_key)

    status: LaunchDarklyImportStatus = {
        "requested_environment_count": ld_project["environments"]["totalCount"],
        "requested_flag_count": requested_flag_count,
    }

    return LaunchDarklyImportRequest.objects.create(
        project=project,
        created_by=user,
        ld_project_key=ld_project_key,
        ld_token=_sign_ld_value(ld_token, user.id),
        status=status,
    )


def process_import_request(
    import_request: LaunchDarklyImportRequest,
) -> None:
    with _complete_import_request(import_request):
        ld_token = _unsign_ld_value(
            import_request.ld_token,
            import_request.created_by.id,
        )
        ld_project_key = import_request.ld_project_key

        ld_client = LaunchDarklyClient(ld_token)

        try:
            ld_environments = ld_client.get_environments(project_key=ld_project_key)
            ld_flags = ld_client.get_flags(project_key=ld_project_key)
            ld_tags = ld_client.get_flag_tags()
        except RequestException as exc:
            _log_error(
                import_request=import_request,
                error_message=(
                    f"{exc.__class__.__name__} "
                    f"{str(exc.response.status_code) + ' ' if exc.response else ''}"
                    f"when requesting {exc.request.path_url}"
                ),
            )
            raise

        environments_by_ld_environment_key = _create_environments_from_ld(
            ld_environments=ld_environments,
            project_id=import_request.project_id,
        )
        tags_by_ld_tag = _create_tags_from_ld(
            ld_tags=ld_tags,
            project_id=import_request.project_id,
        )
        _create_features_from_ld(
            ld_flags=ld_flags,
            environments_by_ld_environment_key=environments_by_ld_environment_key,
            tags_by_ld_tag=tags_by_ld_tag,
            project_id=import_request.project_id,
        )
