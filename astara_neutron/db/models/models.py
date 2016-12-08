# Copyright (c) 2016 Akanda, Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from neutron_lib.db import model_base
import sqlalchemy as sa


class Byonf(model_base.BASEV2, model_base.HasId, model_base.HasProject):
    __tablename__ = 'astara_byonf'
    function_type = sa.Column(sa.String(length=255), nullable=False)
    driver = sa.Column(sa.String(length=36), nullable=False)
    image_uuid = sa.Column(sa.String(length=36), nullable=False)
    __table_args__ = (
        sa.UniqueConstraint(
            'project_id', 'function_type', name='uix_project_id_function'),
    )
